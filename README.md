# Brightspace MCP

An MCP server that connects [Claude Code](https://claude.com/claude-code) to [D2L Brightspace](https://www.d2l.com/brightspace/) LMS. Browse courses, check assignments, download lecture slides, and view grades — all from your terminal.

Includes a **sync skill** that automatically detects and downloads new course materials to your local project folders.

## How It Works

```
Claude Code  --stdio-->  MCP Server  --httpx-->  D2L Brightspace REST API
                              |
                        Playwright  -->  Office365 SSO (login only)
```

- **Login**: Playwright opens a browser for Office365 SSO (supports MFA). Cookies are saved for reuse.
- **API calls**: All data fetching uses the D2L Valence REST API via `httpx` with session cookies.
- **No admin access needed**: Works with regular student/instructor accounts.

## Tools

| Tool | Description |
|------|-------------|
| `login` | Authenticate via Office365 SSO (opens browser, supports MFA) |
| `get_courses` | List all enrolled courses |
| `get_assignments` | Get assignments with due dates and instructions |
| `get_calendar` | Upcoming events and deadlines across all courses |
| `get_course_content` | Browse course content tree (modules, files, links) |
| `download_file` | Download a file to your local filesystem |
| `get_grades` | View your grades for a course |

## Setup

### Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Claude Code](https://claude.com/claude-code)

### Install

```bash
git clone https://github.com/northprot/brightspace-mcp.git
cd brightspace-mcp
uv sync
uv run playwright install chromium
```

### Configure

Create a `.env` file in the project root:

```env
BRIGHTSPACE_URL=https://your-school.brightspace.com
BRIGHTSPACE_USER=your-email@school.edu
BRIGHTSPACE_PASS=your-password
```

### Register with Claude Code

```bash
claude mcp add -s user --transport stdio \
  -e BRIGHTSPACE_URL=https://your-school.brightspace.com \
  -e BRIGHTSPACE_USER=your-email@school.edu \
  -e BRIGHTSPACE_PASS=your-password \
  brightspace -- uv run --directory /path/to/brightspace-mcp brightspace-mcp
```

### First Login

The first time you use any tool, you'll need to authenticate:

1. Claude will call the `login` tool
2. A browser window opens with your school's Office365 login
3. Enter your credentials and complete MFA if prompted
4. Session cookies are saved to `~/.brightspace-mcp/` for future use

Sessions typically last 1-4 hours. The server auto-restores saved sessions on startup and warns when cookies are aging.

## Sync Skill

The `skill/` directory contains a Claude Code skill for **automatic course material sync**.

### Install the Skill

Copy the skill to your Claude Code skills directory:

```bash
cp -r skill/ ~/.claude/skills/brightspace-sync/
```

### Usage

Just tell Claude:
- "sync my courses"
- "check for new materials"
- "download new lecture slides"

The skill will:
1. Scan Brightspace for all course files
2. Compare with your local folders
3. Show you what's new
4. Download and organize new files automatically
5. Optionally convert PPTX slides to PDF

## Configuration Reference

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `BRIGHTSPACE_URL` | `https://d2l.langara.bc.ca` | Your school's Brightspace URL |
| `BRIGHTSPACE_USER` | — | Login email |
| `BRIGHTSPACE_PASS` | — | Login password |
| `BRIGHTSPACE_HEADLESS` | `false` | Set `true` to hide the login browser |
| `BRIGHTSPACE_SESSION_DIR` | `~/.brightspace-mcp/` | Where cookies and session data are stored |
| `BRIGHTSPACE_DOWNLOAD_DIR` | `~/.brightspace-mcp/downloads/` | Default download location |

## Session Management

- Cookies are saved to `~/.brightspace-mcp/storage_state.json`
- Sessions auto-restore on server startup
- If a session expires mid-use, tools automatically attempt to restore from saved cookies
- If restore fails, you'll see "Session expired. Call 'login' to re-authenticate."
- Cookie age warning appears when session is >2.5 hours old

## Compatibility

Tested with:
- D2L Brightspace with Office365 SSO (Microsoft Entra ID)
- API versions: LP 1.57, LE 1.92
- Python 3.14, but should work with 3.12+

The server uses standard D2L Valence REST API endpoints. It should work with any Brightspace instance that exposes these APIs, though the SSO login flow is specific to Microsoft Office365.

## License

MIT
