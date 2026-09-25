# Brightspace MCP

An MCP server that connects [Claude Code](https://claude.com/claude-code) to [D2L Brightspace](https://www.d2l.com/brightspace/) LMS. Browse courses, check assignments, download lecture slides, and view grades — all from your terminal.

Includes a **sync skill** that automatically detects and downloads new course materials to your local project folders.

## How It Works

```
Claude Code  --stdio-->  MCP Server  --httpx-->  D2L Brightspace REST API
                              |
                        Playwright  -->  persistent Chromium profile
                                         (holds the Office365 SSO session)
```

- **Login**: Playwright opens a persistent Chromium profile that already holds
  the Office365 SSO session and re-authenticates silently. No password is
  stored or typed and no MFA prompt appears. The profile is seeded once by
  hand (see First Login); cookies are saved for reuse.
- **API calls**: All data fetching uses the D2L Valence REST API via `httpx` with session cookies.
- **No admin access needed**: Works with regular student/instructor accounts.

## Tools

14 tools, in the order they're defined in `server.py`:

| Tool | Description | Parameters |
|------|-------------|------------|
| `login` | Silent SSO refresh from the persistent browser profile (no credentials, no MFA) | — |
| `get_courses` | List all enrolled courses | — |
| `get_assignments` | List assignment/lab dropbox folders for a course, with due dates and instructions | `course_id` |
| `get_calendar` | Upcoming due dates and events across all active courses | `days` (default 14) |
| `get_course_content` | Browse the content tree: modules and topics at a given level | `course_id`, `module_id` (optional, omit for root) |
| `download_file` | Download a content topic file to the local filesystem | `course_id`, `topic_id`, `save_dir` (optional) |
| `get_assignment_attachments` | List files and links attached to an assignment/lab dropbox folder. A handout attached as a link (not a file) comes back with `file_id` 0 and a `link_url`; fetch that with `download_linked_file`, not `download_assignment_file` | `course_id`, `folder_id` |
| `download_assignment_file` | Download a file attached to an assignment/lab dropbox folder | `course_id`, `folder_id`, `file_id`, `save_dir` (optional) |
| `download_announcement_file` | Download a file attached to an announcement | `course_id`, `news_id`, `file_id`, `save_dir` (optional) |
| `get_grades` | View your own grade values for a course | `course_id` |
| `get_course_overview` | Read the Content tool's Overview page: text, links and attachment info. The Overview is not part of the content tree, so outlines attached here never show up in `get_course_content` | `course_id` |
| `download_course_overview` | Download the file attached to the Content tool's Overview (often the course outline) | `course_id`, `save_dir` (optional) |
| `download_linked_file` | Download a Brightspace file linked from inside an HTML page. Same host only — the session cookies ride on the request | `url`, `save_dir` (optional) |
| `audit_course` | Read every student-visible source of a course (Overview, full module tree including unreleased topics, every HTML page in it, announcements, dropbox folders, quizzes, gradebook setup, calendar, discussions, checklists, staff-only classlist, course home navbar) and report, per source: status (ok/empty/**UNKNOWN**), course outline candidates, AI-policy mentions, due items, content not yet released, documents missing locally with the exact download call for each, files skipped because they're listed in the local `.syncignore`, closed topics past their end date (403, not counted as missing), remote files that changed after the local copy, and same-host links it did not follow. A source that errors is reported as UNKNOWN, never as empty | `course_id`, `local_root` (optional local course directory to diff against) |

Every download tool refuses to overwrite an existing file: a name collision is saved as
`X (1).ext`, `X (2).ext`, etc., and the returned `filename`/`save_path` is the name actually
written — always check it rather than assuming the requested name landed.

Every tool that calls the D2L API treats an expired session as expiry, not as empty data:
D2L answers an expired session with 403 (rarely 401), or with a redirect back to the login
page that arrives as a 200 HTML page after following redirects. Either one raises internally
and triggers one retry from saved cookies before a tool gives up with `"Session expired. Call
'login' to re-authenticate."` — never with an empty list that looks like a course with nothing
in it.

## Setup

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code](https://claude.com/claude-code)

### Install

```bash
git clone https://github.com/cyanprot/brightspace-mcp.git
cd brightspace-mcp
uv sync
uv run playwright install chromium
```

### Configure

Create a `.env` file in the project root:

```env
BRIGHTSPACE_URL=https://your-school.brightspace.com
BRIGHTSPACE_USER=your-email@school.edu
```

No password goes here. The Microsoft SSO session lives in a persistent
Chromium profile that you seed once by hand (see First Login).

### Register with Claude Code

```bash
claude mcp add -s user --transport stdio \
  -e BRIGHTSPACE_URL=https://your-school.brightspace.com \
  -e BRIGHTSPACE_USER=your-email@school.edu \
  brightspace -- xvfb-run -a uv run --directory /path/to/brightspace-mcp brightspace-mcp
```

`BRIGHTSPACE_HEADLESS` defaults to `false`, so the silent refresh still needs a
display. `xvfb-run -a` supplies a virtual one, as in the registration above.
Keep the headful
default: in headless mode Chromium skips the ProcessSingleton profile lock, and
two processes can then write the same profile at once.

### First Login

Seed the browser profile once, by hand, from a real desktop session:

```bash
uv run --directory /path/to/brightspace-mcp brightspace-mcp-login
```

A visible Chromium window opens on your school's Office365 login. Enter your
password, approve MFA, and answer **Yes** to "Stay signed in?" — that answer is
what makes every later login unattended.

After that, the `login` tool re-authenticates **silently** from the profile: no
password is stored or typed, and no MFA prompt appears. Rerun the command above
only when `login` tells you the profile's SSO session has died.

## Sync Skill

The `skill/` directory contains a Claude Code skill for **automatic course material sync**.

### Install the Skill

Copy the skill's contents to your Claude Code skills directory:

```bash
mkdir -p ~/.claude/skills/brightspace-sync
cp skill/SKILL.md ~/.claude/skills/brightspace-sync/
```

Copy the *contents*, not the `skill/` directory itself, or you get a nested
`skill/` inside the target. If that target is a symlink into another repo, the
file behind the symlink is the copy Claude Code loads, and the copy in `skill/`
can lag behind it.

### Usage

Just tell Claude:
- "sync my courses"
- "check for new materials"
- "download new lecture slides"

The skill will:
1. Run `audit_course` first (and again after downloading) — a plain file sync misses
   outlines buried in the Content tool's Overview or linked from inside HTML pages,
   gradebook weights, and quiz due times, so the audit is what proves the sync actually
   complete
2. Scan Brightspace for all course files **and assignment/lab attachments**
3. Compare with your local folders (never re-downloading anything listed in `.syncignore`)
4. Show you what's new
5. Download and organize new files automatically (with folder name normalization)
6. Optionally convert PPTX slides to PDF, and HTML content pages to PDF

## Configuration Reference

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `BRIGHTSPACE_URL` | (required) | Your school's Brightspace URL, e.g. `https://your-school.brightspace.com`. The server refuses to start without it |
| `BRIGHTSPACE_USER` | — | Login email. Only prefills the sign-in box during the manual bootstrap |
| `BRIGHTSPACE_HEADLESS` | `false` | Set `true` to hide the browser during the silent refresh |
| `BRIGHTSPACE_SESSION_DIR` | `~/.local/state/brightspace-mcp/` | Cookies, downloads, and the persistent browser profile |
| `BRIGHTSPACE_DOWNLOAD_DIR` | `~/.local/state/brightspace-mcp/downloads/` | Default download location |
| `BRIGHTSPACE_TZ` | system zone (`TZ`, then `/etc/localtime`), else `UTC` | IANA timezone `audit_course` uses to render due dates and timestamps in local time, e.g. `America/Toronto` |

## Session Management

- The Microsoft SSO session lives in the persistent Chromium profile at
  `~/.local/state/brightspace-mcp/chrome-profile/`. **Treat that directory as a
  credential** — never copy it between machines, never commit it
- Brightspace cookies are saved to `~/.local/state/brightspace-mcp/storage_state.json`
- Sessions auto-restore on server startup
- If a session expires mid-use, tools automatically attempt to restore from saved cookies
- If restore fails, you'll see "Session expired. Call 'login' to re-authenticate."
- `login` refreshes from the profile without credentials. If the profile itself
  has no session left it returns the `brightspace-mcp-login` bootstrap
  instructions instead of hanging on a login form nobody can see
- Cookie age warning appears when session is >2.5 hours old

### Known limitation: the mid-call retry cannot self-heal

`_with_auth_retry` catches `SessionExpiredError` and retries once via
`try_restore_session`, which re-reads the same `storage_state.json` the dead
cookies came from. Absent a concurrent writer it returns identical stale
cookies and fails again, costing one wasted round trip. The failure is loud
(`"Session expired. Call 'login' to re-authenticate."`), so this is a missed
opportunity rather than a hazard. Now that `sso_login()` is unattended and
takes about 2 seconds, calling it here instead would make the retry actually
recover. Deliberately not changed on 2026-08-11: it alters runtime behaviour.

## Compatibility

Tested with:
- D2L Brightspace with Office365 SSO (Microsoft Entra ID)
- API versions: LP 1.57, LE 1.92
- Python 3.14, but should work with 3.12+

The server uses standard D2L Valence REST API endpoints. It should work with any Brightspace instance that exposes these APIs, though the SSO login flow is specific to Microsoft Office365.

## Testing

```bash
uv run pytest
```

The suite (`tests/test_api.py`, `tests/test_audit.py`, `tests/test_auth.py`,
`tests/test_server.py`) covers the API client's pagination, redirect/403 session-expiry
detection, and no-overwrite download naming; the audit report builder; the persistent-profile
login flow; and the MCP tool wrappers. It runs offline against fakes/mocks — no live
Brightspace session or network access is required.

## License

MIT
