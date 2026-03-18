import logging
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .api import BrightspaceAPI, SessionExpiredError
from .auth import sso_login, try_restore_session
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


async def _with_auth_retry(
    app: AppContext, func: Callable[[], Coroutine[Any, Any, Any]]
) -> Any:
    """Run func(), retry once with session restore on 401."""
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
        return await func()


@mcp.tool()
async def login(ctx: Context) -> str:
    """Authenticate to Langara Brightspace via Office365 SSO.

    Opens a browser for login. Call this before using other tools
    if you get an authentication error.
    """
    app = _get_app(ctx)

    await ctx.info("Starting Brightspace SSO login...")
    cookies = await sso_login(app.config)

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
        return f"Error fetching content: {e}"


@mcp.tool()
async def download_file(
    course_id: int, topic_id: int, save_dir: str | None = None, ctx: Context = None
) -> DownloadResult | str:
    """Download a file from course content to local filesystem.

    Args:
        course_id: The course org unit ID.
        topic_id: The topic ID of the file to download. Must be a file-type topic.
        save_dir: Optional directory to save to. Defaults to ~/.brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    target_dir = Path(save_dir) if save_dir else app.config.download_dir
    await ctx.info(f"Downloading topic {topic_id} to {target_dir}...")

    try:
        return await _with_auth_retry(
            app, lambda: app.api.download_topic_file(course_id, topic_id, target_dir)
        )
    except Exception as e:
        return f"Download failed: {e}"


@mcp.tool()
async def get_assignment_attachments(
    course_id: int, folder_id: int, ctx: Context = None
) -> list[DropboxAttachment] | str:
    """List files attached to an assignment/lab dropbox folder.

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
        return f"Error fetching attachments: {e}"


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
        save_dir: Optional directory to save to. Defaults to ~/.brightspace-mcp/downloads/
    """
    app = _get_app(ctx)
    if not app.api:
        return "Not authenticated. Call 'login' first."

    target_dir = Path(save_dir) if save_dir else app.config.download_dir
    await ctx.info(f"Downloading attachment {file_id} to {target_dir}...")

    try:
        return await _with_auth_retry(
            app, lambda: app.api.download_dropbox_attachment(course_id, folder_id, file_id, target_dir)
        )
    except Exception as e:
        return f"Download failed: {e}"


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
        return f"Error fetching grades: {e}"


def main() -> None:
    mcp.run(transport="stdio")
