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


def test_news_attachment_download(tmp_path):
    path = "/d2l/api/le/1.92/7/news/42/attachments/9"
    api, _ = _api({path: (200, b"x", {"content-disposition": 'attachment; filename="info.pdf"'})})
    res = _run(api.download_news_attachment(7, 42, 9, tmp_path))
    assert res.filename == "info.pdf" and (tmp_path / "info.pdf").exists()
