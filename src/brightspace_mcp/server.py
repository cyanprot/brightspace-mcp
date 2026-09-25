import logging
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .api import BrightspaceAPI, SessionExpiredError
from .audit import audit_course as run_audit
from .audit import parse_html, render, strip_query
from .auth import LoginRequiredError, sso_login, try_restore_session
from .config import Config
from .models import Assignment, CalendarEvent, ContentItem, Course, DownloadResult, DropboxAttachment, GradeValue

# All logging goes to stderr — stdout is reserved for MCP stdio transport
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger(__name__)

_COOKIE_WARN_AGE = 9000  # 2.5 hours in seconds


@dataclass
class AppContext:
    config: Config = field(default_factory=Config)
    api: BrightspaceAPI | None = None


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Manage persistent API client across tool calls."""
    config = Config()
    ctx = AppContext(config=config)

    # Try restoring saved session
    cookies = await try_restore_session(config)
    if cookies:
        api = BrightspaceAPI(config, cookies)
        if await api.test_auth():
            ctx.api = api
            age = config.cookie_age_seconds
            if age and age > _COOKIE_WARN_AGE:
                logger.warning(
                    "Cookies are %.1f hours old — session may expire soon",
                    age / 3600,
                )
            logger.info("Restored saved session")
        else:
            await api.close()
            logger.info("Saved session expired")

    try:
        yield ctx
    finally:
        if ctx.api:
            await ctx.api.close()


mcp = FastMCP("Brightspace", lifespan=app_lifespan)


def _get_app(ctx: Context) -> AppContext:
    return ctx.request_context.lifespan_context


def _target_dir(save_dir: str | None, config: Config) -> Path:
    """Where a download lands. `~` is expanded; a relative path is refused.

    Path(save_dir) alone left "~/x" as a literal directory and dropped a relative
    path into the server's cwd, which is this repo, not anywhere the caller meant.
    """
    if not save_dir:
        return config.download_dir
    path = Path(save_dir).expanduser()
    if not path.is_absolute():
        raise ValueError(f"save_dir must be an absolute path (or start with ~): {save_dir!r}")
    return path.resolve()


def _failed(what: str, e: Exception) -> str:
    """'<what>: <ErrorType>: <message>' for a tool's error string.

    The type is always included: a timeout's str() is empty, and "Download failed: "
    alone said nothing about what went wrong.
    """
    return f"{what}: {type(e).__name__}: {e}" if str(e) else f"{what}: {type(e).__name__}"


async def _with_auth_retry(
    app: AppContext, func: Callable[[], Coroutine[Any, Any, Any]]
) -> Any:
    """Run func(), retry once with session restore when the session has expired.

    Every failure path returns a string that says "Session expired" and names
    'login', so a caller never mistakes an auth failure for missing data.
    """
    try:
        return await func()
    except SessionExpiredError:
        logger.warning("Session expired, attempting restore...")
        cookies = await try_restore_session(app.config)
        if not cookies:
            app.api = None
            return "Session expired. Call 'login' to re-authenticate."
        new_api = BrightspaceAPI(app.config, cookies)
        if not await new_api.test_auth():
            await new_api.close()
            app.api = None
            return "Session expired. Call 'login' to re-authenticate."
        if app.api:
            await app.api.close()
        app.api = new_api
        logger.info("Session restored successfully")
    try:
        return await func()
    except SessionExpiredError:
        app.api = None
        return ("Session expired again right after a restore. Call 'login' to "
                "re-authenticate. Nothing was read, so nothing may be recorded as empty.")


@mcp.tool()
async def login(ctx: Context) -> str:
    """Refresh the Brightspace session from the saved browser profile.

    Silent — no credentials, no MFA. Call this before using other tools if you
    get an authentication error. If the profile's SSO session is gone, this
    returns instructions for the one-time manual re-login the user must run.
    """
    app = _get_app(ctx)

    await ctx.info("Refreshing Brightspace session...")
    try:
        cookies = await sso_login(app.config)
    except LoginRequiredError as e:
        return str(e)

    if app.api:
        await app.api.close()
    app.api = BrightspaceAPI(app.config, cookies)
    user = await app.api.whoami()

    name = f"{user.get('FirstName', '')} {user.get('LastName', '')}".strip()
    return f"Logged in as {name}"


@mcp.tool()
async def get_courses(ctx: Context) -> list[Course] | str:
    """List all enrolled courses on Brightspace."""
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."
    return await _with_auth_retry(app, lambda: app.api.get_enrollments())


@mcp.tool()
async def get_assignments(course_id: int, ctx: Context) -> list[Assignment] | str:
    """Get assignments for a specific course.

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        courses = await app.api.get_enrollments()
        course_name = next((c.name for c in courses if c.id == course_id), "")
        return await app.api.get_assignments(course_id, course_name)

    return await _with_auth_retry(app, _fetch)


@mcp.tool()
async def get_calendar(days: int = 14, ctx: Context = None) -> list[CalendarEvent] | str:
    """Get upcoming due dates and events across all courses.

    Args:
        days: Number of days ahead to look. Default 14.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        courses = await app.api.get_enrollments()
        active_ids = [c.id for c in courses if c.is_active]
        return await app.api.get_calendar_events(active_ids, days)

    return await _with_auth_retry(app, _fetch)


@mcp.tool()
async def get_course_content(
    course_id: int, module_id: int | None = None, ctx: Context = None
) -> list[ContentItem] | str:
    """Browse course content. Returns modules and files at the given level.

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
        module_id: Optional module ID to drill into. Omit to see root content.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        if module_id is not None:
            return await app.api.get_module_children(course_id, module_id)
        return await app.api.get_content_root(course_id)

    try:
        return await _with_auth_retry(app, _fetch)
    except Exception as e:
        return _failed("Error fetching content", e)


@mcp.tool()
async def download_file(
    course_id: int, topic_id: int, save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download a file from course content to local filesystem.

    Args:
        course_id: The course org unit ID.
        topic_id: The topic ID of the file to download. Must be a file-type topic.
        save_dir: Optional directory to save to. Defaults to ~/.local/state/brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        target_dir = _target_dir(save_dir, app.config)
        await ctx.info(f"Downloading topic {topic_id} to {target_dir}...")
        return await _with_auth_retry(
            app, lambda: app.api.download_topic_file(course_id, topic_id, target_dir)
        )
    except Exception as e:
        return _failed("Download failed", e)


@mcp.tool()
async def get_assignment_attachments(
    course_id: int, folder_id: int, ctx: Context = None
) -> list[DropboxAttachment] | str:
    """List files attached to an assignment/lab dropbox folder.

    A handout attached as a link comes back with file_id 0 and a link_url: fetch it
    with download_linked_file (Brightspace paths only), not download_assignment_file.

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
        folder_id: The dropbox folder ID. Use get_assignments to find it.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        return await _with_auth_retry(
            app, lambda: app.api.get_dropbox_attachments(course_id, folder_id)
        )
    except Exception as e:
        return _failed("Error fetching attachments", e)


@mcp.tool()
async def download_assignment_file(
    course_id: int, folder_id: int, file_id: int,
    save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download a file attached to an assignment/lab dropbox folder.

    Args:
        course_id: The course org unit ID.
        folder_id: The dropbox folder ID. Use get_assignments to find it.
        file_id: The file ID from get_assignment_attachments.
        save_dir: Optional directory to save to. Defaults to ~/.local/state/brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        target_dir = _target_dir(save_dir, app.config)
        await ctx.info(f"Downloading attachment {file_id} to {target_dir}...")
        return await _with_auth_retry(
            app, lambda: app.api.download_dropbox_attachment(course_id, folder_id, file_id, target_dir)
        )
    except Exception as e:
        return _failed("Download failed", e)


@mcp.tool()
async def download_announcement_file(
    course_id: int, news_id: int, file_id: int,
    save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download a file attached to an announcement.

    audit_course lists announcement attachments with the news_id and file_id to pass.

    Args:
        course_id: The course org unit ID.
        news_id: The announcement ID.
        file_id: The attachment's file ID.
        save_dir: Optional directory to save to. Defaults to ~/.local/state/brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        target_dir = _target_dir(save_dir, app.config)
        return await _with_auth_retry(
            app, lambda: app.api.download_news_attachment(course_id, news_id, file_id, target_dir)
        )
    except Exception as e:
        return _failed("Download failed", e)


@mcp.tool()
async def get_grades(course_id: int, ctx: Context = None) -> list[GradeValue] | str:
    """Get your grades for a specific course.

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        courses = await app.api.get_enrollments()
        course_name = next((c.name for c in courses if c.id == course_id), "")
        return await app.api.get_grades(course_id, course_name)

    try:
        return await _with_auth_retry(app, _fetch)
    except Exception as e:
        return _failed("Error fetching grades", e)


@mcp.tool()
async def get_course_overview(course_id: int, ctx: Context = None) -> dict | str:
    """Read the Content tool's Overview page: its text, links and attachment.

    Instructors often attach the course outline here. The Overview is NOT part of
    the content tree, so get_course_content never shows it.

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        ov = await app.api.get_overview(course_id)
        if ov is None:
            return {"has_overview": False}
        html = (ov.get("Description") or {}).get("Html", "")
        links, text = parse_html(html)
        attachment = None
        if ov.get("HasAttachment"):
            code, name, size = await app.api.overview_attachment_info(course_id)
            attachment = (
                {"filename": name, "size_bytes": size} if code == 200
                else {"status": f"attachment present, unreadable (HTTP {code})"}
            )
        return {
            "has_overview": True,
            "text": text,
            "links": [{"text": t, "href": strip_query(h)} for h, t in links],
            "attachment": attachment,
        }

    try:
        return await _with_auth_retry(app, _fetch)
    except Exception as e:
        return _failed("Error fetching overview", e)


@mcp.tool()
async def download_course_overview(
    course_id: int, save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download the file attached to the Content tool's Overview (usually the outline).

    Args:
        course_id: The course org unit ID.
        save_dir: Optional directory to save to. Defaults to ~/.local/state/brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        target_dir = _target_dir(save_dir, app.config)
        return await _with_auth_retry(
            app, lambda: app.api.download_overview_attachment(course_id, target_dir)
        )
    except Exception as e:
        return _failed("Download failed", e)


@mcp.tool()
async def download_linked_file(
    url: str, save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download a course file that is linked from inside an HTML page, not a topic itself.

    audit_course lists these under "Documents not present locally" with the url to pass.
    Only Brightspace paths are accepted, because the session cookies go with the request.

    Args:
        url: A Brightspace path such as /content/enforced/.../file.pdf, or a full URL on
            the Brightspace host.
        save_dir: Optional directory to save to. Defaults to ~/.local/state/brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    try:
        target_dir = _target_dir(save_dir, app.config)
        return await _with_auth_retry(app, lambda: app.api.download_linked_file(url, target_dir))
    except Exception as e:
        return _failed("Download failed", e)

@mcp.tool()
async def audit_course(
    course_id: int, local_root: str | None = None, ctx: Context = None
) -> str:
    """Audit every student-visible source of a course and report what was checked.

    Reads the Overview, the whole module structure (including topics not yet released)
    and every HTML page in it, announcements, dropbox folders, quizzes, the gradebook
    setup, the course calendar, discussions, checklists, the classlist (staff only)
    and the course home navbar. Returns a markdown report with a status per source,
    navbar tools it could not read, course outline candidates, AI-policy mentions, due
    items in local time, content not yet released, calendar-only events, documents
    missing locally (with the download call for each), remote files changed after the
    local copy, Brightspace links it did not follow, and links out.

    A source that errors is reported as UNKNOWN, never as empty. Run this before
    recording anything about a course as "not posted" or "not declared".

    Args:
        course_id: The course org unit ID. Use get_courses to find it.
        local_root: Optional local course directory. Documents are compared against it
            by filename. `.*` directories are skipped, and so are frozen previous-term
            trees (`_spring2026/`, `_handout/spring2026/`) and every other `_*`
            directory except `_handout/`, which holds this term's handouts.
            Filenames listed in `<local_root>/.syncignore` (deleted by the user) are
            reported as ignored, not missing.
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    async def _fetch():
        courses = await app.api.get_enrollments()
        course_name = next((c.name for c in courses if c.id == course_id), "")
        report = await run_audit(
            app.api, course_id, course_name, Path(local_root).expanduser() if local_root else None
        )
        return render(report)

    try:
        return await _with_auth_retry(app, _fetch)
    except Exception as e:
        return _failed("Audit failed", e)


def main() -> None:
    mcp.run(transport="stdio")
