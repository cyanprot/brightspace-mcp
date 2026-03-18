import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx

from .config import Config
from .models import Assignment, CalendarEvent, ContentItem, Course, DownloadResult, GradeValue


class SessionExpiredError(Exception):
    """Raised when the D2L session has expired (401 response)."""

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

    _TOPIC_TYPE_MAP = {1: "file", 3: "link", 5: "scorm", 6: "scorm", 7: "scorm", 8: "scorm"}

    async def _get(self, path: str) -> dict | list:
        """Make an authenticated GET request to the D2L API."""
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url)
        if resp.status_code == 401:
            raise SessionExpiredError()
        resp.raise_for_status()
        return resp.json()

    async def _get_binary(self, path: str) -> httpx.Response:
        """Make an authenticated GET request expecting binary data."""
        url = f"{self.base_url}{path}"
        resp = await self.client.get(url, timeout=300.0)
        if resp.status_code == 401:
            raise SessionExpiredError()
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

    async def test_auth(self) -> bool:
        """Test if the current session is valid."""
        try:
            await self.whoami()
            return True
        except (httpx.HTTPStatusError, httpx.ConnectError):
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

        # Extract filename from Content-Disposition header
        cd = resp.headers.get("content-disposition", "")
        match = re.search(r'filename\*?=["\']?(?:UTF-8\'\')?([^"\';,\r\n]+)', cd)
        filename = unquote(match.group(1).strip()) if match else f"topic_{topic_id}.bin"
        filename = re.sub(r'[/<>:"\\|?*\x00]', "_", filename)[:200]

        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / filename

        # Handle filename collision
        if path.exists():
            stem, suffix = path.stem, path.suffix
            n = 1
            while path.exists():
                path = save_dir / f"{stem} ({n}){suffix}"
                n += 1

        path.write_bytes(resp.content)

        return DownloadResult(
            topic_id=topic_id,
            filename=filename,
            save_path=str(path),
            size_bytes=len(resp.content),
        )

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
