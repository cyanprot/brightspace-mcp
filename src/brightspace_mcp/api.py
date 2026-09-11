import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx

from .config import Config
from .models import Assignment, CalendarEvent, ContentItem, Course, DownloadResult, DropboxAttachment, GradeValue


class SessionExpiredError(Exception):
    """Raised when the D2L session has expired (401, or a 403 while whoami also fails)."""

logger = logging.getLogger(__name__)
logger.addHandler(logging.StreamHandler(sys.stderr))


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

    async def _raise_if_expired(self, resp: httpx.Response) -> None:
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
        """
        if resp.status_code == 401:
            raise SessionExpiredError()
        if resp.status_code == 403 and "not authorized" not in resp.text.lower():
            probe = await self.client.get(
                f"{self.base_url}/d2l/api/lp/{self.config.lp_version}/users/whoami"
            )
            if probe.status_code != 200:
                raise SessionExpiredError()

    _TOPIC_TYPE_MAP = {1: "file", 3: "link", 5: "scorm", 6: "scorm", 7: "scorm", 8: "scorm"}

    async def _get(self, path: str) -> dict | list:
        """Make an authenticated GET request to the D2L API."""
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url)
        await self._raise_if_expired(resp)
        resp.raise_for_status()
        return resp.json()

    async def _get_binary(self, path: str) -> httpx.Response:
        """Make an authenticated GET request expecting binary data."""
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url, timeout=300.0)
        await self._raise_if_expired(resp)
        resp.raise_for_status()
        return resp

    async def _get_all_pages(self, path: str) -> list[dict]:
        """GET with ObjectListPage pagination support."""
        data = await self._get(path)
        if isinstance(data, list):
            return data
        items = data.get("Objects", data.get("Items", []))
        next_url = data.get("Next")
        while next_url:
            if next_url.startswith("http"):
                resp = await self.client.get(next_url)
                await self._raise_if_expired(resp)
                resp.raise_for_status()
                data = resp.json()
            else:
                data = await self._get(next_url)
            if isinstance(data, list):
                items.extend(data)
                break
            items.extend(data.get("Objects", data.get("Items", [])))
            next_url = data.get("Next")
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
        cd = resp.headers.get("content-disposition", "")
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';,\r\n]+)', cd)
        filename = unquote(match.group(1).strip()) if match else fallback
        return re.sub(r'[/<>:"\\|?*\x00]', "_", filename)[:200]

    def _save_response(
        self, resp: httpx.Response, save_dir: Path, item_id: int, fallback: str
    ) -> DownloadResult:
        """Write a binary response to save_dir, never overwriting an existing file."""
        filename = self.filename_from_response(resp, fallback)
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / filename

        if path.exists():
            stem, suffix = path.stem, path.suffix
            n = 1
            while path.exists():
                path = save_dir / f"{stem} ({n}){suffix}"
                n += 1

        path.write_bytes(resp.content)

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
        rather than silently treating an error as an empty source. An expired session
        still raises (see _raise_if_expired), so the session-retry wrapper can refresh
        the login.
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
        resp = await self._get_binary(url)
        fallback = unquote(url.split("?", 1)[0].rsplit("/", 1)[-1]) or "linked_file.bin"
        if resp.headers.get("content-type", "").startswith("text/html"):
            raise ValueError(f"{url!r} returned an HTML page, not a file")
        return self._save_response(resp, save_dir, 0, fallback)

    async def test_auth(self) -> bool:
        """Test if the current session is valid."""
        try:
            await self.whoami()
            return True
        except (SessionExpiredError, httpx.HTTPStatusError, httpx.ConnectError):
            return False

    async def whoami(self) -> dict:
        """GET /d2l/api/lp/{ver}/users/whoami — returns current user info."""
        return await self._get(f"/d2l/api/lp/{self.config.lp_version}/users/whoami")

    async def get_enrollments(self) -> list[Course]:
        """GET /d2l/api/lp/{ver}/enrollments/myenrollments/ — list enrolled courses."""
        data = await self._get(
            f"/d2l/api/lp/{self.config.lp_version}/enrollments/myenrollments/"
            "?orgUnitTypeId=3"
        )

        items = data if isinstance(data, list) else data.get("Items", [])
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
        data = await self._get(
            f"/d2l/api/le/{self.config.le_version}/{course_id}/dropbox/folders/"
        )

        items = data if isinstance(data, list) else []
        assignments = []
        for item in items:
            due = item.get("DueDate")
            instructions = item.get("Instructions", {})
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

        csv_ids = ",".join(str(cid) for cid in course_ids[:20])

        try:
            data = await self._get(
                f"/d2l/api/le/{self.config.le_version}/"
                f"calendar/events/myEvents/"
                f"?startDateTime={start}&endDateTime={end}"
                f"&orgUnitIdsCSV={csv_ids}"
            )
        except httpx.HTTPStatusError:
            # Fallback: try without orgUnitIdsCSV filter
            data = await self._get(
                f"/d2l/api/le/{self.config.le_version}/"
                f"calendar/events/myEvents/"
                f"?startDateTime={start}&endDateTime={end}"
            )

        items = data if isinstance(data, list) else data.get("Objects", [])
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
        attachments = data.get("Attachments", []) if isinstance(data, dict) else []
        return [
            DropboxAttachment(
                file_id=att.get("FileId", 0),
                folder_id=folder_id,
                filename=att.get("FileName", ""),
                size_bytes=att.get("Size", 0),
            )
            for att in attachments
        ]

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
