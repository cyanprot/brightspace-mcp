"""Tests for the HTTP layer: how responses are classified and how files are saved.

The failure these pin down: D2L answers an expired session with 403, not 401. When
that 403 was read as "this resource is forbidden", an expired session looked like a
course with nothing in it.
"""

import asyncio

import httpx
import pytest

from brightspace_mcp.api import BrightspaceAPI, SessionExpiredError

BASE = "https://d2l.example.ca"
WHOAMI = "/d2l/api/lp/1.57/users/whoami"


class _Config:
    brightspace_url = BASE
    le_version = "1.92"
    lp_version = "1.57"


def _api(routes: dict):
    """API whose client answers from routes: path -> (status, body, headers). Unlisted = 404."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        status, body, headers = routes.get(request.url.path, (404, b"", {}))
        return httpx.Response(status, content=body, headers=headers)

    api = BrightspaceAPI(_Config(), [])
    asyncio.run(api.client.aclose())
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api, calls


def _run(coro):
    return asyncio.run(coro)


JSON = {"content-type": "application/json; charset=UTF-8"}
HTML = {"content-type": "text/html; charset=utf-8"}
NOT_AUTHORIZED = (403, b'{"Errors":[{"Message":"Not Authorized"}]}', JSON)
AUTH_REQUIRED = (403, b"Authentication required", HTML)
WHOAMI_OK = (200, b'{"FirstName":"A"}', JSON)


# --- session expiry ----------------------------------------------------------


def test_401_is_expired():
    api, _ = _api({"/x": (401, b"", {})})
    with pytest.raises(SessionExpiredError):
        _run(api.get_status_json("/x"))


def test_403_with_dead_session_is_expired_not_forbidden():
    api, _ = _api({"/x": AUTH_REQUIRED, WHOAMI: (403, b'{ Errors: [ {Message: "Forbidden"} ] }', HTML)})
    with pytest.raises(SessionExpiredError):
        _run(api.get_status_json("/x"))
    with pytest.raises(SessionExpiredError):
        _run(api.get_text("/x"))
    with pytest.raises(SessionExpiredError):
        _run(api.download_linked_file("/x", None))


def test_permission_403_stays_a_status_and_skips_the_probe():
    api, calls = _api({"/x": NOT_AUTHORIZED})
    assert _run(api.get_status_json("/x")) == (403, None)
    assert WHOAMI not in calls


def test_unfamiliar_403_on_live_session_stays_a_status():
    # The body is not trusted alone: whoami decides.
    api, calls = _api({"/x": (403, b"odd", HTML), WHOAMI: WHOAMI_OK})
    assert _run(api.get_status_json("/x")) == (403, None)
    assert WHOAMI in calls


def test_test_auth_false_on_expired_403():
    api, _ = _api({WHOAMI: AUTH_REQUIRED})
    assert _run(api.test_auth()) is False


def test_non_json_200_is_reported_not_swallowed():
    api, _ = _api({"/x": (200, b"<html>login</html>", HTML)})
    assert _run(api.get_status_json("/x")) == (-1, None)


def test_test_auth_false_on_timeout_and_captive_portal():
    # Both used to escape test_auth and crash app_lifespan, so the server never
    # started and 'login' was unreachable.
    api, _ = _api({WHOAMI: (200, b"<html>hotel wifi</html>", HTML)})
    assert _run(api.test_auth()) is False

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    assert _run(api.test_auth()) is False


# --- overview attachment -----------------------------------------------------

ATT = "/d2l/api/le/1.92/7/overview/attachment"


def test_overview_attachment_info_reports_status():
    api, _ = _api({ATT: (200, b"x" * 2048, {"content-disposition": 'attachment; filename="Outline.pdf"'})})
    assert _run(api.overview_attachment_info(7)) == (200, "Outline.pdf", 2048)
    api, _ = _api({ATT: NOT_AUTHORIZED})
    assert _run(api.overview_attachment_info(7)) == (403, "", 0)


# --- saving files ------------------------------------------------------------


def test_download_linked_file_accepts_full_url_and_rejects_html(tmp_path):
    path = "/content/enforced/1-X/a.pdf"
    api, _ = _api({path: (200, b"%PDF", {"content-type": "application/pdf"}),
                   "/content/enforced/1-X/page": (200, b"<html></html>", HTML)})
    res = _run(api.download_linked_file(BASE + path, tmp_path))
    assert res.filename == "a.pdf" and (tmp_path / "a.pdf").read_bytes() == b"%PDF"
    with pytest.raises(ValueError, match="HTML page"):
        _run(api.download_linked_file("/content/enforced/1-X/page", tmp_path))


def test_collision_reports_the_name_actually_written(tmp_path):
    # The second download of a revised file lands as "a (1).pdf". Reporting "a.pdf"
    # hid that a duplicate now sits next to the original.
    path = "/content/enforced/1-X/a.pdf"
    api, _ = _api({path: (200, b"%PDF", {"content-type": "application/pdf"})})
    first = _run(api.download_linked_file(path, tmp_path))
    second = _run(api.download_linked_file(path, tmp_path))
    assert first.filename == "a.pdf"
    assert second.filename == "a (1).pdf" and second.save_path.endswith("a (1).pdf")


def test_long_filename_keeps_its_extension():
    resp = httpx.Response(200, headers={"content-disposition": f'attachment; filename="{"L" * 300}.pdf"'})
    name = BrightspaceAPI.filename_from_response(resp, "x.bin")
    assert len(name) == 200 and name.endswith(".pdf") and name.startswith("LLL")
    assert BrightspaceAPI.filename_from_response(httpx.Response(200), "topic_1.bin") == "topic_1.bin"


def test_save_never_follows_a_dangling_symlink(tmp_path):
    # exists() is False for a dangling symlink, and write_bytes() would have
    # followed it and created the target instead of a file in save_dir.
    path = "/content/enforced/1-X/a.pdf"
    api, _ = _api({path: (200, b"%PDF", {"content-type": "application/pdf"})})
    (tmp_path / "a.pdf").symlink_to(tmp_path / "elsewhere" / "target.pdf")
    res = _run(api.download_linked_file(path, tmp_path))
    assert res.filename == "a (1).pdf"
    assert (tmp_path / "a (1).pdf").read_bytes() == b"%PDF"
    assert not (tmp_path / "elsewhere").exists()


def test_download_linked_file_refuses_a_redirect_off_host(tmp_path):
    # The path check runs before the GET. A redirect is followed, and the cookies
    # would ride along to wherever it lands.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "d2l.example.ca":
            return httpx.Response(302, headers={"location": "https://evil.example.com/a.pdf"})
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(ValueError, match="redirected off the Brightspace host"):
        _run(api.download_linked_file("/content/enforced/1-X/a.pdf", tmp_path))
    assert not (tmp_path / "a.pdf").exists()


def test_news_attachment_download(tmp_path):
    path = "/d2l/api/le/1.92/7/news/42/attachments/9"
    api, _ = _api({path: (200, b"x", {"content-disposition": 'attachment; filename="info.pdf"'})})
    res = _run(api.download_news_attachment(7, 42, 9, tmp_path))
    assert res.filename == "info.pdf" and (tmp_path / "info.pdf").exists()


# --- Content-Disposition (RFC 6266) ----------------------------------------


def _cd(value: str) -> str:
    return BrightspaceAPI.filename_from_response(httpx.Response(200, headers={"content-disposition": value}), "x.bin")


@pytest.mark.parametrize(
    "header, expected",
    [
        # Each of these was cut short by the old single regex.
        ('attachment; filename="Video Newton\'s Laws.html"', "Video Newton's Laws.html"),
        ('attachment; filename="Lab 3, Part A.pdf"', "Lab 3, Part A.pdf"),
        ('attachment; filename="a; b.pdf"', "a; b.pdf"),
        ("attachment; filename*=utf-8''a%20b.pdf", "a b.pdf"),
        ("attachment; filename*=UTF-8'en'%C3%A9t%C3%A9.pdf", "été.pdf"),
        ("attachment; filename*=iso-8859-1''%E9t%E9.pdf", "été.pdf"),
        ("attachment; filename=plain.pdf; size=3", "plain.pdf"),
        # Backslash escapes inside a quoted-string; the quote is then sanitised away.
        ('attachment; filename="say \\"hi\\".pdf"', "say _hi_.pdf"),
    ],
)
def test_content_disposition_filename(header, expected):
    assert _cd(header) == expected


def test_filename_star_wins_over_plain_filename_in_either_order():
    assert _cd("attachment; filename=\"fallback.pdf\"; filename*=UTF-8''Real%20Name.pdf") == "Real Name.pdf"
    assert _cd("attachment; filename*=UTF-8''Real%20Name.pdf; filename=\"fallback.pdf\"") == "Real Name.pdf"
    # An unknown charset falls back to the plain form instead of returning garbage.
    assert _cd("attachment; filename=\"ok.pdf\"; filename*=x-nope''%FF.pdf") == "ok.pdf"
    assert _cd('attachment; filename=""') == "x.bin"


# --- login redirects -----------------------------------------------------------


LOGIN = "/d2l/login"


def _redirecting(routes: dict):
    """API whose client follows redirects, like the real one. routes: path -> Response."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "d2l.example.ca":
            return httpx.Response(200, content=b"<html>Sign in</html>", headers=HTML)
        return routes.get(request.url.path, httpx.Response(404))

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    return api


def _to(location: str) -> httpx.Response:
    return httpx.Response(302, headers={"location": location})


TOPIC = "/d2l/api/le/1.92/7/content/topics/5/file"
LOGIN_PAGE = httpx.Response(200, content=b"<html>Sign in</html>", headers=HTML)


def test_download_redirected_to_login_is_expired_not_saved(tmp_path):
    # With redirects followed, the login page arrived as a 200 and was saved as the file.
    api = _redirecting({TOPIC: _to(f"{BASE}{LOGIN}?sessionExpired=1"), LOGIN: LOGIN_PAGE})
    with pytest.raises(SessionExpiredError):
        _run(api.download_topic_file(7, 5, tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("call", [
    lambda api, d: api.download_dropbox_attachment(7, 3, 9, d),
    lambda api, d: api.download_news_attachment(7, 42, 9, d),
    lambda api, d: api.download_overview_attachment(7, d),
    lambda api, d: api.get_status_json("/d2l/api/le/1.92/7/news/"),
    lambda api, d: api.get_text("/d2l/home/7"),
])
def test_every_path_treats_a_redirect_to_the_sso_host_as_expired(call, tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "d2l.example.ca":
            return _to("https://login.sso.example.com/saml?x=1")
        return httpx.Response(200, content=b"<html>Sign in</html>", headers=HTML)

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    with pytest.raises(SessionExpiredError):
        _run(call(api, tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_status_json_redirected_to_login_is_expired_not_http_minus_1():
    api = _redirecting({"/x": _to(LOGIN), LOGIN: LOGIN_PAGE})
    with pytest.raises(SessionExpiredError):
        _run(api.get_status_json("/x"))


def test_linked_file_through_login_is_expired(tmp_path):
    api = _redirecting({"/content/enforced/1-X/a.pdf": _to(LOGIN), LOGIN: LOGIN_PAGE})
    with pytest.raises(SessionExpiredError):
        _run(api.download_linked_file("/content/enforced/1-X/a.pdf", tmp_path))


def test_same_host_redirect_that_is_not_login_is_fine(tmp_path):
    api = _redirecting({TOPIC: _to("/content/enforced/1-X/a.pdf"),
                        "/content/enforced/1-X/a.pdf": httpx.Response(200, content=b"%PDF")})
    res = _run(api.download_topic_file(7, 5, tmp_path))
    assert (tmp_path / res.filename).read_bytes() == b"%PDF"


def test_whoami_probe_redirected_to_login_counts_as_expired():
    # A 403 with an unfamiliar body is checked against whoami. A whoami that was
    # redirected to the login page is a 200 too, and read as "session alive".
    api = _redirecting({"/x": httpx.Response(403, content=b"odd", headers=HTML),
                        WHOAMI: _to(LOGIN), LOGIN: LOGIN_PAGE})
    with pytest.raises(SessionExpiredError):
        _run(api.get_status_json("/x"))


# --- list shapes and pagination ------------------------------------------------


def _json_api(pages: dict):
    """API answering from pages: 'path?query' (or bare path) -> JSON body. Records requests."""
    import json as _json
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        seen.append(key)
        body = pages.get(key, pages.get(request.url.path))
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=_json.dumps(body).encode(), headers=JSON)

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return api, seen


ENROL = "/d2l/api/lp/1.57/enrollments/myenrollments/"


def _course(i):
    return {"OrgUnit": {"Id": i, "Name": f"C{i}"}, "Access": {"IsActive": True}}


def test_enrollments_follow_the_bookmark():
    # PagedResultSet: the second page is only reachable through PagingInfo.Bookmark.
    api, seen = _json_api({
        f"{ENROL}?orgUnitTypeId=3": {"PagingInfo": {"Bookmark": "b1", "HasMoreItems": True}, "Items": [_course(1)]},
        f"{ENROL}?orgUnitTypeId=3&bookmark=b1": {"PagingInfo": {"Bookmark": "b2", "HasMoreItems": False},
                                                 "Items": [_course(2)]},
    })
    assert [c.id for c in _run(api.get_enrollments())] == [1, 2]
    assert len(seen) == 2


def test_object_list_page_follows_next():
    root = "/d2l/api/le/1.92/7/content/root/"
    api, _ = _json_api({
        root: {"Objects": [{"Type": 0, "Id": 1, "Title": "A"}], "Next": f"{BASE}/page2"},
        "/page2": {"Objects": [{"Type": 0, "Id": 2, "Title": "B"}], "Next": None},
    })
    assert [i.id for i in _run(api.get_content_root(7))] == [1, 2]


@pytest.mark.parametrize("body", [{"Something": []}, {"Objects": None}, "text"])
def test_unrecognised_list_shape_raises_not_empty(body):
    api, _ = _json_api({ENROL: body})
    with pytest.raises(ValueError, match="unrecognised list response"):
        _run(api.get_enrollments())


def test_more_items_without_a_bookmark_raises():
    api, _ = _json_api({ENROL: {"PagingInfo": {"Bookmark": "", "HasMoreItems": True}, "Items": []}})
    with pytest.raises(ValueError, match="HasMoreItems without a Bookmark"):
        _run(api.get_enrollments())


def test_calendar_batches_course_ids_instead_of_dropping_them():
    ids = list(range(1, 46))  # 45 courses: three batches of 20, 20, 5
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        csv = request.url.params.get("orgUnitIdsCSV", "")
        calls.append(csv)
        first = int(csv.split(",")[0])
        body = {"Objects": [{"CalendarEventId": first, "Title": f"E{first}"}], "Next": None}
        import json as _json
        return httpx.Response(200, content=_json.dumps(body).encode(), headers=JSON)

    api, _ = _api({})
    api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = _run(api.get_calendar_events(ids))
    assert [len(c.split(",")) for c in calls] == [20, 20, 5]
    assert "45" in calls[-1].split(",")
    assert [e.id for e in events] == [1, 21, 41]


def test_assignments_read_object_list_custom_instructions_and_hidden():
    folders = "/d2l/api/le/1.92/7/dropbox/folders/"
    api, _ = _json_api({folders: {"Objects": [
        {"Id": 1, "Name": "Lab 1", "IsHidden": True, "CustomInstructions": {"Text": "Read me", "Html": "<p>Read me</p>"}},
        {"Id": 2, "Name": "Lab 2", "Instructions": {"Text": "Old field"}},
    ], "Next": None}})
    a1, a2 = _run(api.get_assignments(7))
    assert (a1.name, a1.is_hidden, a1.instructions_snippet) == ("Lab 1", True, "Read me")
    assert (a2.is_hidden, a2.instructions_snippet) == (False, "Old field")


def test_dropbox_attachments_include_link_attachments():
    folder = "/d2l/api/le/1.92/7/dropbox/folders/3"
    api, _ = _json_api({folder: {"Attachments": [{"FileId": 9, "FileName": "a.pdf", "Size": 4}],
                                 "LinkAttachments": [{"LinkId": 1, "LinkName": "Handout",
                                                      "Href": "/content/enforced/7-X/handout.pdf"}]}})
    files = _run(api.get_dropbox_attachments(7, 3))
    assert [(f.file_id, f.filename, f.link_url) for f in files] == [
        (9, "a.pdf", None), (0, "Handout", "/content/enforced/7-X/handout.pdf")]
