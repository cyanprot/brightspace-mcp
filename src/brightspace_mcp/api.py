import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from .config import Config
from .models import Assignment, CalendarEvent, ContentItem, Course, DownloadResult, DropboxAttachment, GradeValue


class SessionExpiredError(Exception):
    """Raised when the D2L session has expired (401, or a 403 while whoami also fails)."""

logger = logging.getLogger(__name__)

# myEvents takes the course ids as one CSV query parameter. Batched, never truncated:
# course_ids[:20] used to drop every course past the 20th without a word.
CALENDAR_BATCH = 20

# One Content-Disposition parameter: `; key=value`, the value a quoted-string (which
# may hold `;`, `,` and `'`) or a bare token running to the next `;`.
_CD_PARAM_RE = re.compile(
    r'(?:^|;)\s*(?P<key>[^\s=;"]+)\s*=\s*(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<token>[^;]*))'
)


def disposition_filename(cd: str) -> str | None:
    """The filename in a Content-Disposition header, per RFC 6266, or None.

    `filename*` (RFC 8187: charset'language'percent-encoded) wins over `filename`,
    because a server sends both and the plain one is the ASCII fallback. The old
    single regex stopped at the first `'`, `,` or `;`, so "Newton's Laws.html" came
    back as "Newton", and it returned the charset ("utf-8") for a lower-case
    `filename*=utf-8''...`.
    """
    params: dict[str, str] = {}
    for m in _CD_PARAM_RE.finditer(cd or ""):
        if m.group("quoted") is not None:
            value = re.sub(r"\\(.)", r"\1", m.group("quoted"), flags=re.DOTALL)
        else:
            value = m.group("token").strip()
        params.setdefault(m.group("key").lower(), value)
    ext = re.fullmatch(r"([^']*)'[^']*'(.*)", params.get("filename*", ""), re.DOTALL)
    if ext:
        try:
            name = unquote(ext.group(2), encoding=ext.group(1) or "utf-8", errors="strict")
        except (LookupError, UnicodeDecodeError):
            name = ""  # unknown charset or bytes that are not in it: use the plain form
        if name.strip():
            return name
    plain = params.get("filename", "")
    # The plain form is percent-decoded too, as it always was here: RFC 6266 says
    # not to, but some servers percent-encode it and browsers decode it.
    return unquote(plain) if plain.strip() else None


def page_items(data) -> list | None:
    """The items on one page of a D2L list, or None for a shape this code does not know.

    Three shapes are real: a bare JSON array, an ObjectListPage (`Objects`, `Next`) and
    a PagedResultSet (`Items`, `PagingInfo`). `data.get("Objects") or data.get("Items")
    or []` read any other 200 as an empty list, so a changed response would have looked
    like a course with nothing in it.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("Objects", "Items"):
            if isinstance(data.get(key), list):
                return data[key]
    return None


def next_page(path: str, data) -> str | None:
    """Path or URL of the page after `data`, or None when `data` is the last page.

    ObjectListPage carries a `Next` URL. PagedResultSet carries `PagingInfo.Bookmark`
    and `HasMoreItems`, and the next page is the same request with `bookmark=` set.
    Raises ValueError when a page says there is more but not where it is.
    """
    if not isinstance(data, dict):
        return None
    if data.get("Next"):
        return data["Next"]
    paging = data.get("PagingInfo")
    if not isinstance(paging, dict) or not paging.get("HasMoreItems"):
        return None
    if not paging.get("Bookmark"):
        raise ValueError(f"{path.split('?', 1)[0]}: HasMoreItems without a Bookmark")
    parts = urlsplit(path)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "bookmark"]
    query.append(("bookmark", str(paging["Bookmark"])))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _shape(data) -> str:
    return f"keys {sorted(data)[:6]}" if isinstance(data, dict) else type(data).__name__


class BrightspaceAPI:
    """Async HTTP client for D2L Valence REST API using session cookies."""

    def __init__(self, config: Config, cookies: list[dict]) -> None:
        self.config = config
        self.base_url = config.brightspace_url

        jar = httpx.Cookies()
        for c in cookies:
            jar.set(c["name"], c["value"], domain=c.get("domain", ""))

        self.client = httpx.AsyncClient(
            cookies=jar,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 BrightspaceMCP/0.1"},
        )

    def _is_login_redirect(self, resp: httpx.Response, off_host_is_login: bool = True) -> bool:
        """Did a followed redirect end at the login page instead of the resource?

        The client follows redirects, so an expired session can answer with a 302 to
        /d2l/login (and on to the SSO host) that arrives as a 200 HTML page. A download
        then saved the login page as the file and reported success, and the audit
        reported "UNKNOWN (HTTP -1)" without ever suggesting a login.

        `off_host_is_login=False` is for a caller-supplied path that may legitimately
        redirect elsewhere (a quickLink to an external site): only a hop through
        /d2l/login counts there.
        """
        if not resp.history:
            return False
        chain = [r.url for r in resp.history] + [resp.url]
        if any(u.path.lower().startswith("/d2l/login") for u in chain):
            return True
        host = (urlsplit(self.base_url).hostname or "").lower()
        return off_host_is_login and (resp.url.host or "").lower() != host

    async def _raise_if_expired(self, resp: httpx.Response, off_host_is_login: bool = True) -> None:
        """Raise SessionExpiredError when a response means the session is gone.

        D2L rarely answers an expired session with 401. It sends 403 with the body
        "Authentication required", or an HTML-typed "Forbidden" when no session cookie
        goes out at all (observed 2026-09-10, whoami included). A real permission
        refusal on a live session is 403 with JSON "Not Authorized". Treating the
        expired case as a permission error turned every audit source into UNKNOWN and
        every sync call into "log and continue", with the fix (login) never suggested.

        The body text alone is not trusted: any other 403 is confirmed against whoami,
        so a permission 403 with an unfamiliar body stays a per-resource error instead
        of aborting a whole audit.

        A redirect to the login page is expiry too (see _is_login_redirect), including
        on the whoami probe, whose redirected login page would otherwise be a 200.
        """
        if self._is_login_redirect(resp, off_host_is_login):
            raise SessionExpiredError()
        if resp.status_code == 401:
            raise SessionExpiredError()
        if resp.status_code == 403 and "not authorized" not in resp.text.lower():
            probe = await self.client.get(
                f"{self.base_url}/d2l/api/lp/{self.config.lp_version}/users/whoami"
            )
            if probe.status_code != 200 or self._is_login_redirect(probe):
                raise SessionExpiredError()

    _TOPIC_TYPE_MAP = {1: "file", 3: "link", 5: "scorm", 6: "scorm", 7: "scorm", 8: "scorm"}

    async def _get(self, path: str) -> dict | list:
        """Make an authenticated GET request to the D2L API.

        A full URL is taken as is: an ObjectListPage's `Next` link is absolute.
        """
        url = path if path.startswith(("http://", "https://")) else f"{self.base_url}{path}"
        resp = await self.client.get(url)
        await self._raise_if_expired(resp)
        resp.raise_for_status()
        return resp.json()

    async def _get_binary(self, path: str, off_host_is_login: bool = True) -> httpx.Response:
        """Make an authenticated GET request expecting binary data."""
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url, timeout=300.0)
        await self._raise_if_expired(resp, off_host_is_login)
        resp.raise_for_status()
        return resp

    async def _get_all_pages(self, path: str) -> list[dict]:
        """GET a D2L list and every further page of it (see page_items, next_page).

        A 200 of an unrecognised shape raises ValueError instead of reading as empty.
        """
        items: list = []
        seen: set[str] = set()
        while path:
            if path in seen:
                raise ValueError(f"pagination loops back to {path.split('?', 1)[0]}")
            seen.add(path)
            data = await self._get(path)
            page = page_items(data)
            if page is None:
                raise ValueError(f"unrecognised list response from {path.split('?', 1)[0]}: {_shape(data)}")
            items.extend(page)
            path = next_page(path, data)
        return items

    @staticmethod
    def _parse_content_items(raw_items: list[dict]) -> list[ContentItem]:
        """Convert raw D2L JSON objects to ContentItem list."""
        items = []
        for obj in raw_items:
            d2l_type = obj.get("Type", 0)
            items.append(
                ContentItem(
                    id=obj.get("Id", 0),
                    title=obj.get("Title", ""),
                    item_type="module" if d2l_type == 0 else "topic",
                    topic_type=BrightspaceAPI._TOPIC_TYPE_MAP.get(obj.get("TopicType"))
                    if d2l_type == 1
                    else None,
                    url=obj.get("Url"),
                    parent_module_id=obj.get("ParentModuleId"),
                    due_date=obj.get("DueDate"),
                    last_modified=obj.get("LastModifiedDate"),
                    is_hidden=obj.get("IsHidden", False),
                    has_children=(d2l_type == 0),
                )
            )
        return items

    @staticmethod
    def filename_from_response(resp: httpx.Response, fallback: str) -> str:
        """Filename from Content-Disposition, sanitised for the local filesystem."""
        name = disposition_filename(resp.headers.get("content-disposition", ""))
        filename = re.sub(r'[/<>:"\\|?*\x00]', "_", name.strip() if name else fallback)
        # Truncate the stem, not the whole name: cutting the extension off a long
        # "Lecture ... .pdf" left a file nothing would open.
        suffix = Path(filename).suffix if len(Path(filename).suffix) <= 16 else ""
        stem = filename[: len(filename) - len(suffix)]
        return stem[: 200 - len(suffix)] + suffix

    def _save_response(
        self, resp: httpx.Response, save_dir: Path, item_id: int, fallback: str
    ) -> DownloadResult:
        """Write a binary response to save_dir, never overwriting an existing file."""
        filename = self.filename_from_response(resp, fallback)
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / filename
        stem, suffix = path.stem, path.suffix

        # O_EXCL claims the name in the same step that tests it, and O_NOFOLLOW
        # refuses a symlink there: exists() is False for a dangling one, and
        # write_bytes() would then have followed it and written elsewhere.
        n = 0
        while True:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
                break
            except FileExistsError:
                n += 1
                path = save_dir / f"{stem} ({n}){suffix}"
        with os.fdopen(fd, "wb") as fh:
            fh.write(resp.content)

        # Report the name actually written. A collision saves "X (1).pdf", and
        # reporting "X.pdf" hid that a second copy now sits next to the first.
        return DownloadResult(
            topic_id=item_id,
            filename=path.name,
            save_path=str(path),
            size_bytes=len(resp.content),
        )

    async def get_status_json(self, path: str) -> tuple[int, dict | list | None]:
        """GET that reports failure as a status code instead of raising.

        Used by the course audit, which must record "this source could not be read"
        rather than silently treating an error as an empty source. An expired session,
        including a redirect to the login page, still raises (see _raise_if_expired),
        so the session-retry wrapper can refresh the login.
        """
        resp = await self.client.get(f"{self.base_url}{path}")
        await self._raise_if_expired(resp)
        if resp.status_code != 200:
            return resp.status_code, None
        try:
            return 200, resp.json()
        except ValueError:
            return -1, None

    async def get_text(self, path: str) -> tuple[int, str]:
        """GET a text resource (an HTML content topic, the course home page)."""
        resp = await self.client.get(f"{self.base_url}{path}")
        await self._raise_if_expired(resp)
        return resp.status_code, resp.text if resp.status_code == 200 else ""

    async def get_overview(self, course_id: int) -> dict | None:
        """GET /d2l/api/le/{ver}/{courseId}/overview — the Content tool's Overview.

        Instructors often attach the course outline here, and it is NOT part of the
        content tree, so walking modules and topics never finds it. Returns None when
        the course has no overview (HTTP 404).
        """
        status, data = await self.get_status_json(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/overview"
        )
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"overview returned HTTP {status}")
        return data

    async def overview_attachment_info(self, course_id: int) -> tuple[int, str, int]:
        """(HTTP status, filename, size) of the Overview attachment.

        The status is returned rather than folded into None: the caller already knows
        an attachment exists (HasAttachment), so a failed GET means "unreadable", not
        "no attachment". Filename and size are only meaningful when the status is 200.
        """
        resp = await self.client.get(
            f"{self.base_url}/d2l/api/le/{self.config.le_version}/{course_id}/overview/attachment"
        )
        await self._raise_if_expired(resp)
        if resp.status_code != 200:
            return resp.status_code, "", 0
        return 200, self.filename_from_response(resp, "overview_attachment.bin"), len(resp.content)

    async def download_overview_attachment(
        self, course_id: int, save_dir: Path
    ) -> DownloadResult:
        """Download the file attached to the Content tool's Overview."""
        resp = await self._get_binary(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/overview/attachment"
        )
        return self._save_response(resp, save_dir, course_id, f"overview_{course_id}.bin")

    async def download_linked_file(self, url: str, save_dir: Path) -> DownloadResult:
        """Download a course file linked from an HTML page, announcement or overview.

        Only Brightspace-hosted paths are accepted. The session cookies ride on this
        request, so an arbitrary host would receive them.
        """
        url = url.removeprefix(self.base_url)
        if not url.startswith("/") or url.startswith("//"):
            raise ValueError(f"not a Brightspace path: {url!r}")
        # A hop through /d2l/login is an expired session. Landing on another host
        # without one is not: a quickLink can point off-site. That stays a refusal.
        resp = await self._get_binary(url, off_host_is_login=False)
        # Redirects are followed, so the path check above is not the last word.
        host = urlsplit(self.base_url).hostname
        if resp.url.host.lower() != (host or "").lower():
            raise ValueError(f"{url!r} redirected off the Brightspace host to {resp.url.host!r}")
        fallback = unquote(url.split("?", 1)[0].rsplit("/", 1)[-1]) or "linked_file.bin"
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise ValueError(f"{url!r} returned an HTML page, not a file")
        return self._save_response(resp, save_dir, 0, fallback)

    async def test_auth(self) -> bool:
        """Test if the current session is valid."""
        try:
            await self.whoami()
            return True
        except (SessionExpiredError, httpx.HTTPError, ValueError):
            # Any transport failure (timeouts included) and a 200 that is not JSON
            # (a captive portal) mean "not usable now", not a crash at startup.
            # The server must still come up so that 'login' stays reachable.
            return False

    async def whoami(self) -> dict:
        """GET /d2l/api/lp/{ver}/users/whoami — returns current user info."""
        return await self._get(f"/d2l/api/lp/{self.config.lp_version}/users/whoami")

    async def get_enrollments(self) -> list[Course]:
        """GET /d2l/api/lp/{ver}/enrollments/myenrollments/ — list enrolled courses."""
        # A PagedResultSet: past the first page only PagingInfo.Bookmark leads on.
        items = await self._get_all_pages(
            f"/d2l/api/lp/{self.config.lp_version}/enrollments/myenrollments/"
            "?orgUnitTypeId=3"
        )
        courses = []
        for item in items:
            org = item.get("OrgUnit", item)
            courses.append(
                Course(
                    id=org.get("Id", org.get("OrgUnitId", 0)),
                    name=org.get("Name", ""),
                    code=org.get("Code"),
                    is_active=item.get("Access", {}).get("IsActive", True)
                    if isinstance(item.get("Access"), dict)
                    else True,
                )
            )
        return courses

    async def get_assignments(self, course_id: int, course_name: str = "") -> list[Assignment]:
        """GET /d2l/api/le/{ver}/{courseId}/dropbox/folders/ — list assignment dropboxes."""
        items = await self._get_all_pages(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/dropbox/folders/"
        )
        assignments = []
        for item in items:
            due = item.get("DueDate")
            # DropboxFolder names this CustomInstructions (the audit reads the same).
            instructions = item.get("CustomInstructions") or item.get("Instructions") or {}
            snippet = None
            if isinstance(instructions, dict):
                text = instructions.get("Text", instructions.get("Html", ""))
                snippet = text[:500] if text else None

            assignments.append(
                Assignment(
                    id=item.get("Id", 0),
                    name=item.get("Name", ""),
                    course_name=course_name,
                    due_date=due,
                    points=item.get("OutOf"),
                    instructions_snippet=snippet,
                    is_hidden=bool(item.get("IsHidden", False)),
                )
            )
        return assignments

    async def get_calendar_events(
        self, course_ids: list[int], days: int = 14
    ) -> list[CalendarEvent]:
        """GET /d2l/api/le/{ver}/calendar/events/myEvents/ — upcoming events."""
        now = datetime.now(timezone.utc)
        start = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        end = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        path = (
            f"/d2l/api/le/{self.config.le_version}/calendar/events/myEvents/"
            f"?startDateTime={start}&endDateTime={end}"
        )
        try:
            items = []
            for i in range(0, len(course_ids), CALENDAR_BATCH):
                csv_ids = ",".join(str(cid) for cid in course_ids[i : i + CALENDAR_BATCH])
                items += await self._get_all_pages(f"{path}&orgUnitIdsCSV={csv_ids}")
        except httpx.HTTPStatusError:
            # Fallback: without the orgUnitIdsCSV filter, which covers every course.
            items = await self._get_all_pages(path)

        events = []
        for item in items:
            events.append(
                CalendarEvent(
                    id=item.get("CalendarEventId", 0),
                    title=item.get("Title", ""),
                    course_name=item.get("OrgUnitName", ""),
                    course_id=item.get("OrgUnitId", 0),
                    start_date=item.get("StartDateTime"),
                    end_date=item.get("EndDateTime"),
                    is_all_day=item.get("IsAllDay", False),
                )
            )
        return events

    async def get_content_root(self, course_id: int) -> list[ContentItem]:
        """GET /d2l/api/le/{ver}/{courseId}/content/root/ — list root modules."""
        raw = await self._get_all_pages(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/content/root/"
        )
        return self._parse_content_items(raw)

    async def get_module_children(self, course_id: int, module_id: int) -> list[ContentItem]:
        """GET /d2l/api/le/{ver}/{courseId}/content/modules/{moduleId}/structure/"""
        raw = await self._get_all_pages(
            f"/d2l/api/le/{self.config.le_version}/{course_id}"
            f"/content/modules/{module_id}/structure/"
        )
        return self._parse_content_items(raw)

    async def download_topic_file(
        self, course_id: int, topic_id: int, save_dir: Path
    ) -> DownloadResult:
        """Download a file topic to local filesystem."""
        resp = await self._get_binary(
            f"/d2l/api/le/{self.config.le_version}/{course_id}"
            f"/content/topics/{topic_id}/file?stream=false"
        )

        return self._save_response(resp, save_dir, topic_id, f"topic_{topic_id}.bin")

    async def get_dropbox_attachments(
        self, course_id: int, folder_id: int
    ) -> list[DropboxAttachment]:
        """GET /d2l/api/le/{ver}/{courseId}/dropbox/folders/{folderId} — list folder attachments."""
        data = await self._get(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/dropbox/folders/{folder_id}"
        )
        if not isinstance(data, dict):
            raise TypeError(f"dropbox folder {folder_id}: unrecognised response, {_shape(data)}")
        files = [
            DropboxAttachment(
                file_id=att.get("FileId", 0),
                folder_id=folder_id,
                filename=att.get("FileName", ""),
                size_bytes=att.get("Size", 0),
            )
            for att in data.get("Attachments") or []
        ]
        # A handout attached as a link sits in LinkAttachments, not Attachments, and
        # was never listed. It has no file id: fetch it by link_url instead.
        links = [
            DropboxAttachment(
                file_id=0,
                folder_id=folder_id,
                filename=link.get("LinkName") or link.get("Href", ""),
                size_bytes=0,
                link_url=link.get("Href"),
            )
            for link in data.get("LinkAttachments") or []
        ]
        return files + links

    async def download_dropbox_attachment(
        self, course_id: int, folder_id: int, file_id: int, save_dir: Path
    ) -> DownloadResult:
        """Download a file attached to a dropbox folder."""
        resp = await self._get_binary(
            f"/d2l/api/le/{self.config.le_version}/{course_id}"
            f"/dropbox/folders/{folder_id}/attachments/{file_id}"
        )

        return self._save_response(resp, save_dir, file_id, f"attachment_{file_id}.bin")

    async def download_news_attachment(
        self, course_id: int, news_id: int, file_id: int, save_dir: Path
    ) -> DownloadResult:
        """Download a file attached to an announcement."""
        resp = await self._get_binary(
            f"/d2l/api/le/{self.config.le_version}/{course_id}"
            f"/news/{news_id}/attachments/{file_id}"
        )
        return self._save_response(resp, save_dir, file_id, f"announcement_{file_id}.bin")

    async def get_grades(self, course_id: int, course_name: str = "") -> list[GradeValue]:
        """GET /d2l/api/le/{ver}/{courseId}/grades/values/myGradeValues/"""
        raw = await self._get_all_pages(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/grades/values/myGradeValues/"
        )
        grades = []
        for item in raw:
            grades.append(
                GradeValue(
                    grade_item_id=str(item.get("GradeObjectIdentifier", "")),
                    name=item.get("GradeObjectName", ""),
                    points_numerator=item.get("PointsNumerator"),
                    points_denominator=item.get("PointsDenominator"),
                    displayed_grade=item.get("DisplayedGrade"),
                    grade_type=item.get("GradeObjectType", 1),
                )
            )
        return grades

    async def close(self) -> None:
        await self.client.aclose()
