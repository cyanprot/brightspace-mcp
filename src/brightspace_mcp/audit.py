"""Course audit: read every student-visible source of a course and report coverage.

Why this exists: a content sync walks modules and topics and downloads files. That is
not the whole course. Outlines hide in the Content tool's Overview (not a topic), in
links inside HTML stub pages, and in announcements; grade weights live in the
gradebook; quiz deadlines live in the quiz tool. Each of those was missed in practice,
and each miss was then written down as "not posted".

The audit's contract is the opposite of a sync's: it must say what it checked, and it
must never turn "could not read" into "empty". Every source gets a status. Navbar tools
the audit has no reader for are listed by name, so the gap is visible.

Contents of PDF/DOCX files are not scanned. Outline candidates are listed so they can
be downloaded and read.
"""

import os
import posixpath
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from .api import BrightspaceAPI

LOCAL_TZ = ZoneInfo(os.environ.get("BRIGHTSPACE_TZ", "America/Vancouver"))

# Files worth having locally. Code and data files count: a lab page links its
# starter .java or .csv the same way it links a PDF handout.
DOC_EXTENSIONS = (
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".txt", ".rtf", ".odt", ".ods", ".odp",
    ".zip", ".gz", ".tgz", ".7z", ".rar", ".jar",
    ".java", ".py", ".ipynb", ".c", ".cpp", ".h", ".js", ".csv", ".json", ".xml", ".md", ".dat",
)
HTML_EXTENSIONS = (".html", ".htm")
MAX_HTML_TOPICS = 80
MAX_MODULES = 300
# Overview text at least this long is listed as an outline candidate even without a
# keyword: a pasted outline need not contain the word "outline".
OVERVIEW_TEXT_CANDIDATE = 800

OUTLINE_RE = re.compile(r"outline|syllabus|course\s+info", re.IGNORECASE)
# "AI" is matched case-sensitively so ordinary words ("said", "maintain") never hit.
AI_RE = re.compile(
    r"\bA\.?I\b|\bgen\s?-?AI\b|artificial intelligence|chat\s?gpt|generative|\bLLMs?\b"
    r"|large language models?|copilot|chatbot|gemini|claude|deepseek|perplexity|grammarly",
    re.IGNORECASE,
)
AI_CASE_RE = re.compile(r"\bA\.?I\b")

# Classlist roles that may be reported. An allowlist, not a denylist of "student":
# Auditor, Guest and any role a college invents are classmates too, and their names
# must never leave the audit.
STAFF_ROLE_RE = re.compile(
    r"instructor|teacher|professor|faculty|teaching assistant|\bTA\b|demonstrator|"
    r"lab technician|marker|grader|coordinator|tutor",
    re.IGNORECASE,
)

# The only query keys a quickLink needs. Everything else is dropped from reported
# links, because a D2L query can carry d2lSessionVal.
QUICKLINK_KEEP = {"ou", "type", "rcode", "fileid"}

# Navbar entries the audit reads. Anything else in the navbar is reported as unaudited.
COVERED_NAV = {
    "course home",
    "content",
    "announcements",
    "assignments",
    "quizzes",
    "calendar",
    "classlist",
    "grades",
    "discussions",
    "checklist",
    "checklists",
}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None

    def handle_data(self, data):
        self.text_parts.append(data)
        if self._href is not None:
            self._text.append(data)


def parse_html(html: str) -> tuple[list[tuple[str, str]], str]:
    """Return ([(href, link text)], visible text) for an HTML fragment."""
    parser = _LinkParser()
    parser.feed(html or "")
    parser.close()
    return parser.links, " ".join(" ".join(parser.text_parts).split())


def strip_query(url: str) -> str:
    """Drop query string and fragment. D2L links can carry tokens in the query."""
    return url.split("?", 1)[0].split("#", 1)[0]


def safe_ref(path: str, query: str) -> str:
    """A same-host path fit for a report: no query, except the ids a quickLink needs."""
    if "quicklink" not in path.lower() or not query:
        return path
    kept = [(k, v) for k, v in parse_qsl(query) if k.lower() in QUICKLINK_KEEP]
    return path + (f"?{urlencode(kept)}" if kept else "")


def url_filename(url: str) -> str:
    return unquote(urlsplit(strip_query(url)).path.rsplit("/", 1)[-1])


def is_document_link(href: str) -> bool:
    """A link that points at a file worth having locally, or at a D2L course file."""
    lower = href.lower()
    if "quicklink" in lower and "coursefile" in lower:
        return True
    return strip_query(lower).endswith(DOC_EXTENSIONS) or "/content/enforced/" in lower


def is_outline(*names: str) -> bool:
    return any(OUTLINE_RE.search(n or "") for n in names)


def ai_mentions(text: str, width: int = 90) -> list[str]:
    """Snippets around every AI-related term in plain text."""
    hits = []
    for m in AI_RE.finditer(text or ""):
        term = m.group(0)
        if term.replace(".", "").upper() == "AI" and not AI_CASE_RE.fullmatch(term):
            continue  # lower-case "ai" is not the acronym
        start, end = max(0, m.start() - width), min(len(text), m.end() + width)
        hits.append(("..." if start else "") + text[start:end].strip() + ("..." if end < len(text) else ""))
    return hits


def words(s: str) -> tuple[str, ...]:
    """'Assignment 02' -> ('assignment', '2'). Leading zeros are dropped."""
    return tuple(str(int(w)) if w.isdigit() else w for w in re.findall(r"[a-z]+|\d+", (s or "").lower()))


def same_place(context: str, parts: tuple[tuple[str, ...], ...]) -> bool:
    """Does a local directory path name the remote folder or module `context`?

    One side must be a prefix of the other ("Week 1: Intro" and "week01"), and a
    partial match needs a number in it, so "labs" never stands for "Lab 3" and
    "lab1" never stands for "lab10".
    """
    ctx = words(context)
    for comp in parts:
        if not comp or not ctx:
            continue
        short, long_ = sorted((ctx, comp), key=len)
        if short == long_[: len(short)] and (short == long_ or any(w.isdigit() for w in short)):
            return True
    return False


@dataclass
class LocalFile:
    parts: tuple[tuple[str, ...], ...]  # directory components relative to the root, as words()
    mtime: float
    size: int


def local_index(root: Path | None) -> dict[str, list[LocalFile]] | None:
    """Lower-cased filename -> every local copy, skipping `_*` and `.*` directories.

    `_*` skips frozen previous-term trees such as `_spring2026/`: a file that only
    exists in last term's tree does not count as present for this term.
    """
    if root is None or not root.is_dir():
        return None
    index: dict[str, list[LocalFile]] = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(("_", "."))]
        rel = Path(dirpath).relative_to(root).parts
        parts = tuple(words(p) for p in rel)
        for f in filenames:
            try:
                st = os.stat(os.path.join(dirpath, f))
            except OSError:
                continue  # a dangling symlink is not a local copy
            index[f.lower()].append(LocalFile(parts, st.st_mtime, st.st_size))
    return dict(index)


def to_local(iso: str | None) -> str:
    """ISO UTC timestamp from the API -> 'Mon 2026-09-14 23:59 PDT' in LOCAL_TZ.

    Never slice the ISO string for a date: 23:59 Pacific is 06:59Z the next day.
    The zone abbreviation is kept on purpose. tz rules change (BC moved to permanent
    UTC-7 in 2026, tzdata 2026c), and a browser with older rules shows a different
    wall-clock time for the same instant. Seeing "MST" vs "PST" makes that visible.
    """
    if not iso:
        return "-"
    dt = datetime.fromisoformat(iso).astimezone(LOCAL_TZ)
    return dt.strftime("%a %Y-%m-%d %H:%M %Z")


def parse_nav(home_html: str) -> list[tuple[str, str]]:
    """Navbar tools from the course home page, de-duplicated, in order."""
    seen: dict[str, str] = {}
    for tag in re.findall(r"<d2l-menu-item-link\b[^>]*>", home_html or ""):
        text = re.search(r'\btext="([^"]*)"', tag)
        href = re.search(r'\bhref="([^"]*)"', tag)
        if text:
            name = " ".join(text.group(1).split())
            seen.setdefault(name, strip_query(href.group(1)) if href else "")
    return list(seen.items())


def rich_html(value) -> str:
    """HTML out of a D2L RichText value, which may be nested one level ({"Text": {...}})."""
    if isinstance(value, dict):
        if isinstance(value.get("Html"), str):
            return value["Html"]
        return rich_html(value.get("Text"))
    return ""


def norm_title(s: str) -> str:
    return re.sub(r"\W+", " ", s or "").strip().lower()


# ---------------------------------------------------------------------------
# Report model
# ---------------------------------------------------------------------------


@dataclass
class Source:
    name: str
    status: str  # "ok", "empty", "none", or "UNKNOWN (...)"
    detail: str = ""


@dataclass
class Doc:
    where: str
    name: str
    local: bool | None = None  # None = not compared (no local root, or not a file name)
    href: str = ""  # where a document linked from HTML lives
    external: bool = False  # href is on another host: fetch it without the session
    ref: str = ""  # the download tool call that fetches it, with its ids
    context: str = ""  # module or folder title, tells same-named files apart
    key: str = ""  # identity of the remote file, so one file linked twice counts once
    modified: str = ""  # remote LastModifiedDate (content topics)
    size: int | None = None  # remote size in bytes (dropbox and announcement attachments)
    compare: bool = False  # take part in the local comparison
    note: str = ""


@dataclass
class Report:
    course: str
    course_id: int
    checked_at: str
    local_root: str | None
    sources: list[Source] = field(default_factory=list)
    nav_unaudited: list[str] = field(default_factory=list)
    outline: list[Doc] = field(default_factory=list)
    ai: list[tuple[str, str]] = field(default_factory=list)
    docs: list[Doc] = field(default_factory=list)
    missing_docs: list[Doc] = field(default_factory=list)
    revised: list[Doc] = field(default_factory=list)
    scheduled: list[tuple[str, str, str]] = field(default_factory=list)  # iso, when, topic
    unreadable_topics: list[str] = field(default_factory=list)
    internal_links: list[tuple[str, str, str]] = field(default_factory=list)  # where, text, path
    external_links: list[tuple[str, str, str]] = field(default_factory=list)
    due: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # iso, due, end, kind, name
    calendar_only: list[tuple[str, str, str]] = field(default_factory=list)  # iso, when, title
    staff: list[str] = field(default_factory=list)
    grading: list[str] = field(default_factory=list)

    def unknown(self) -> list[Source]:
        return [s for s in self.sources if s.status.startswith("UNKNOWN")]


def render(r: Report) -> str:
    out = [f"# Course audit: {r.course} ({r.course_id})", ""]
    ok = sum(s.status == "ok" for s in r.sources)
    out.append(
        f"Checked {r.checked_at}. {len(r.sources)} sources: {ok} with items, "
        f"{sum(s.status in ('empty', 'none') for s in r.sources)} empty, "
        f"**{len(r.unknown())} UNKNOWN**."
    )
    if r.local_root:
        out.append(f"Local tree compared: `{r.local_root}` (`_*` and `.*` dirs skipped).")
    elif not any(s.name == "local tree" for s in r.sources):
        out.append("No local tree given, so no document was compared and none is listed as missing.")
    out += ["", "## Sources", "", "| Source | Status | Detail |", "|---|---|---|"]
    out += [f"| {s.name} | {s.status} | {s.detail} |" for s in r.sources]

    if r.unknown():
        out += ["", "⚠ **UNKNOWN is not empty.** These sources could not be read, so nothing may be "
                "concluded about them: " + ", ".join(s.name for s in r.unknown())]

    out += ["", "## Navbar tools this audit does not read", ""]
    out.append(", ".join(r.nav_unaudited) + ". Open them in a browser if they could hold course "
               "information." if r.nav_unaudited else "None. Every navbar tool was read.")

    out += ["", "## Course outline candidates", ""]
    if r.outline:
        out += [f"- [{d.where}] {d.name}{_local_mark(d.local)}{_fetch_hint(d)}{_note(d)}" for d in r.outline]
        out.append("")
        out.append("File contents are not scanned. **Read each candidate** for the AI policy, "
                   "grade weights and attendance or lab rules before recording any of them.")
    else:
        read = [s.name for s in r.sources if not s.status.startswith("UNKNOWN")]
        out.append(f"⚠ **None found** in: {', '.join(read)}.")
        if r.unknown():
            out.append(f"Not searchable because unreadable: {', '.join(s.name for s in r.unknown())}.")
        out.append("This means \"not found by this audit\", not \"not posted\".")

    out += ["", "## AI mentions in HTML sources", ""]
    if r.ai:
        out += [f"- [{where}] {snip}" for where, snip in r.ai]
    else:
        out.append("None in any HTML source read. PDF and DOCX files were not scanned.")

    if r.staff:
        out += ["", "## Staff (classlist, recognised staff roles only)", ""] + [f"- {s}" for s in r.staff]
    if r.grading:
        out += ["", "## Gradebook setup", ""] + [f"- {g}" for g in r.grading]

    out += ["", f"## Due items ({LOCAL_TZ.key})", ""]
    if r.due:
        out += ["| Due | Closes | Kind | Item |", "|---|---|---|---|"]
        out += [f"| {due} | {end} | {kind} | {name} |" for _, due, end, kind, name in sorted(r.due)]
    else:
        out.append("None.")

    if r.scheduled:
        out += ["", "## Content not yet released", "",
                ("Visible in the module structure with a future start date. Its file 404s until then. "
                 "Re-run the audit after the date.")]
        out += [f"- {when}: {topic}" for _, when, topic in sorted(r.scheduled)]

    if r.calendar_only:
        out += ["", "## Calendar-only events", "",
                ("Events with no matching quiz or dropbox. Often a content release time, which is "
                 "why a topic can 404 before it.")]
        out += [f"- {when}: {title}" for _, when, title in sorted(set(r.calendar_only))]

    if r.unreadable_topics:
        out += ["", "## Released topics that could not be read", ""]
        out += [f"- {t}" for t in r.unreadable_topics]

    if r.missing_docs:
        out += ["", "## Documents not present locally", ""]
        out += [f"- [{d.where}] {d.name}{_fetch_hint(d)}{_note(d)}" for d in r.missing_docs]

    if r.revised:
        out += ["", "## Remote files changed after the local copy", "",
                ("Present locally by name, but the remote copy is newer or a different size. "
                 "Re-download and compare before relying on the local file.")]
        out += [f"- [{d.where}] {d.name}{_fetch_hint(d)}{_note(d)}" for d in r.revised]

    if r.internal_links:
        out += ["", "## Brightspace links not followed", "",
                ("Same-host links that are neither a document nor a topic this audit walked. "
                 "Open each one: it may be the only route to a page or file.")]
        out += [f"- [{where}] {text or '(no text)'} -> `{path}`" for where, text, path in r.internal_links]

    if r.external_links:
        out += ["", "## Links out of HTML sources", ""]
        out += [f"- [{where}] {text or '(no text)'} -> {href}" for where, text, href in r.external_links]

    return "\n".join(out)


def _fetch_hint(d: Doc) -> str:
    if d.ref:
        return f" (`{d.ref}`)"
    if not d.href:
        return ""
    if d.external:
        return f" (external, open without the session: {d.href})"
    return f" (download_linked_file url: `{d.href}`)"


def _note(d: Doc) -> str:
    return f", {d.note}" if d.note else ""


def _local_mark(local: bool | None) -> str:
    return {True: " (present locally)", False: " (**NOT present locally**)", None: ""}[local]


# ---------------------------------------------------------------------------
# Local comparison
# ---------------------------------------------------------------------------


def resolve_local(docs: list[Doc], local: dict[str, list[LocalFile]] | None) -> None:
    """Decide which remote documents exist locally, by file name.

    When one name lives in several remote places (Tester.java in two assignments,
    homework.pdf in two weeks), one local copy cannot stand for all of them. The
    folder or module name then has to appear in the local path, and every remote copy
    that no local file accounts for is missing.
    """
    if local is None:
        return
    groups: dict[str, dict[str, list[Doc]]] = defaultdict(lambda: defaultdict(list))
    for d in docs:
        if d.compare:
            groups[d.name.lower()][d.key or d.where].append(d)
    for name, by_key in groups.items():
        files = list(local.get(name, []))
        n_remote, n_local = len(by_key), len(files)
        if n_local >= n_remote:
            for group in by_key.values():
                for d in group:
                    d.local = True
            continue
        for group in by_key.values():
            hit = next((f for f in files if same_place(group[0].context, f.parts)), None)
            if hit:
                files.remove(hit)
            for d in group:
                d.local = hit is not None
                if hit is None and n_remote > 1:
                    d.note = f"same name in {n_remote} places, {n_local} local, no local folder matches"


def find_revised(docs: list[Doc], local: dict[str, list[LocalFile]] | None) -> list[Doc]:
    """Documents present by name whose remote copy is newer or a different size.

    A download stamps the local file with the download time, so a remote
    LastModifiedDate later than every local copy means it changed after the download.
    """
    if local is None:
        return []
    out = []
    for d in docs:
        files = local.get(d.name.lower(), [])
        if not d.local or not files:
            continue
        if d.modified:
            changed = datetime.fromisoformat(d.modified).timestamp()
            if changed > max(f.mtime for f in files) + 60:
                d.note = f"remote changed {to_local(d.modified)}"
                out.append(d)
        elif d.size and all(f.size != d.size for f in files):
            d.note = f"remote {d.size} bytes, local {', '.join(str(f.size) for f in files)}"
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _status(code: int, data) -> str:
    if code != 200:
        return f"UNKNOWN (HTTP {code})"
    return "ok" if data else "empty"


async def get_list(api: BrightspaceAPI, path: str) -> tuple[int, list]:
    """A D2L list, following ObjectListPage `Next` links. Returns the first failing status."""
    code, data = await api.get_status_json(path)
    items: list = []
    while code == 200:
        if isinstance(data, list):
            items.extend(data)
            break
        if not isinstance(data, dict):
            return -1, items
        items.extend(data.get("Objects") or data.get("Items") or [])
        nxt = data.get("Next")
        if not nxt:
            break
        code, data = await api.get_status_json(nxt.replace(api.base_url, ""))
    return code, items


def _after(iso: str, now: datetime) -> bool:
    return datetime.fromisoformat(iso) > now


async def audit_course(
    api: BrightspaceAPI, course_id: int, course_name: str = "", local_root: Path | None = None,
    now: datetime | None = None,
) -> Report:
    le = f"/d2l/api/le/{api.config.le_version}/{course_id}"
    now = now or datetime.now(UTC)
    local = local_index(local_root)
    r = Report(
        course=course_name or str(course_id),
        course_id=course_id,
        checked_at=now.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z"),
        local_root=str(local_root) if local is not None else None,
    )
    if local_root is not None:
        # A mistyped root must not quietly turn "compare against local files" off:
        # the report would then list 0 missing documents and look complete.
        r.sources.append(
            Source("local tree", "ok", f"{sum(map(len, local.values()))} files under `{local_root}`")
            if local is not None
            else Source("local tree", "UNKNOWN (path not found)",
                        f"`{local_root}` is not a directory, so no document was compared")
        )
    seen_docs: set[tuple[str, str]] = set()

    def add_doc(where: str, name: str, outline_hint: str = "", comparable: bool = True, **extra) -> None:
        if (where, name) in seen_docs:
            return
        seen_docs.add((where, name))
        doc = Doc(where, name, compare=comparable and bool(name), **extra)
        r.docs.append(doc)
        if is_outline(name, outline_hint):
            r.outline.append(doc)

    host = urlsplit(api.base_url).netloc

    def classify_link(where: str, label: str, full: str, context: str = "") -> None:
        """File a resolved link as a document, an unfollowed Brightspace link, or a link out."""
        parts = urlsplit(full)
        same_host = parts.netloc == host
        # urljoin leaves "a/../b" alone when href is already absolute, so normalise here.
        path = posixpath.normpath(parts.path) if parts.path else "/"
        local_ref = safe_ref(path, parts.query) if same_host else strip_query(full)
        if is_document_link(full):
            fname = url_filename(full)
            comparable = fname.lower().endswith(DOC_EXTENSIONS)
            add_doc(where, fname if comparable else (label or fname), outline_hint=label,
                    comparable=comparable, href=local_ref, external=not same_host,
                    key=local_ref, context=context)
        elif not same_host:
            if parts.scheme.startswith("http"):
                r.external_links.append((where, label, local_ref))
            if is_outline(label):
                r.outline.append(Doc(where, label, None, local_ref, True))
        elif re.search(r"[?&]type=(quiz|dropbox)\b", local_ref, re.IGNORECASE):
            return  # quiz and dropbox quickLinks point at tools this audit reads on its own
        elif is_outline(label, path):
            # A page, not a file: download_linked_file would get HTML back.
            r.outline.append(Doc(where, label or path, ref=f"open in a browser: {local_ref}"))
        elif (where, local_ref) not in {(w, p) for w, _, p in r.internal_links}:
            r.internal_links.append((where, label, local_ref))

    def scan_html(where: str, html: str, page_path: str = "/") -> int:
        """Record AI mentions and every link found in an HTML body.

        Relative hrefs ("intro.pdf", "../x.pdf") are resolved against the page they
        sit in. Resolving them is what makes a handout linked from a lab page visible.
        """
        links, text = parse_html(html)
        for snip in ai_mentions(text):
            r.ai.append((where, snip))
        for href, label in links:
            if not href or href.startswith(("mailto:", "javascript:", "#", "tel:")):
                continue
            classify_link(f"link in {where}", label, urljoin(api.base_url + page_path, href))
        return len(links)

    # Overview: the Content tool's front page. Not part of the content tree.
    code, ov = await api.get_status_json(f"{le}/overview")
    if code == 404:
        r.sources.append(Source("overview", "none", "no Overview configured"))
    elif code != 200:
        r.sources.append(Source("overview", f"UNKNOWN (HTTP {code})"))
    else:
        detail = []
        status = ""
        n = scan_html("overview", (ov.get("Description") or {}).get("Html", ""))
        if n:
            detail.append(f"{n} links")
        if ov.get("HasAttachment"):
            att_code, name, size = await api.overview_attachment_info(course_id)
            if att_code == 200:
                detail.append(f"attachment `{name}` ({size // 1024} KB)")
                # In practice the Overview attachment is the outline, whatever its name.
                add_doc("overview attachment", name, outline_hint="course outline",
                        ref="download_course_overview", key="overview-attachment", size=size)
            else:
                # The attachment exists but could not be read. It is still the most likely
                # outline, so it stays a candidate, and the source is not "ok".
                status = f"UNKNOWN (attachment HTTP {att_code})"
                detail.append("attachment present, unreadable")
                r.outline.append(Doc("overview attachment", "(unreadable)", ref="download_course_overview"))
        text = (ov.get("Description") or {}).get("Text", "").strip()
        if text:
            detail.append(f"text {len(text)} chars")
            # An outline pasted straight into the Overview has no file and no link.
            if len(text) >= OVERVIEW_TEXT_CANDIDATE or OUTLINE_RE.search(text):
                r.outline.append(Doc("overview text", f"Overview body, {len(text)} chars",
                                     ref="get_course_overview"))
        r.sources.append(Source("overview", status or ("ok" if detail else "empty"), ", ".join(detail)))

    # Content: content/root plus modules/{id}/structure, the same walk a sync does.
    # content/toc is NOT used: it silently drops topics with a future start date and
    # topics past their end date (one course: 28 topics in toc, 36 in the structure).
    html_topics: list[tuple[int, str, str]] = []
    tree = defaultdict(int)
    bad_modules: list[str] = []

    def topic(o: dict, path: str, module: str) -> None:
        tree["topics"] += 1
        tid, title, url = o.get("Id"), o.get("Title", ""), o.get("Url") or ""
        where = f"content {path}"
        if o.get("IsHidden"):
            tree["hidden"] += 1
            return
        if o.get("IsBroken"):
            tree["broken"] += 1
            r.unreadable_topics.append(f"{path}/{title} (marked broken)")
        start, end = o.get("StartDate"), o.get("EndDate")
        if start and _after(start, now):
            tree["not yet released"] += 1
            r.scheduled.append((start, to_local(start), f"{path}/{title}"))
            if is_outline(title):
                r.outline.append(Doc(where, title, note=f"not released until {to_local(start)}"))
            return
        if end and not _after(end, now):
            tree["closed"] += 1
        if o.get("TopicType") == 3:  # link topic
            tree["link-type"] += 1
            full = urljoin(api.base_url + "/", url)
            classify_link(where, title, full, context=module)
            return
        fname = url_filename(url)
        add_doc(where, fname, outline_hint=title, ref=f"download_file topic_id={tid}",
                key=strip_query(url) or f"topic:{tid}", context=module,
                modified=o.get("LastModifiedDate") or "")
        if fname.lower().endswith(HTML_EXTENSIONS):
            html_topics.append((tid, f"{path}/{title}", url))

    async def walk(module_id: int, path: str, module: str) -> None:
        tree["modules"] += 1
        if tree["modules"] > MAX_MODULES:
            bad_modules.append(f"{path} (skipped, over {MAX_MODULES} modules)")
            return
        code, children = await get_list(api, f"{le}/content/modules/{module_id}/structure/")
        if code != 200:
            bad_modules.append(f"{path} (HTTP {code})")
            return
        for o in children:
            if o.get("Type") == 0:
                await walk(o.get("Id"), f"{path}/{o.get('Title', '')}", o.get("Title", ""))
            else:
                topic(o, path, module)

    code, roots = await get_list(api, f"{le}/content/root/")
    if code != 200:
        r.sources.append(Source("content tree", f"UNKNOWN (HTTP {code})"))
    else:
        for m in roots:
            if m.get("Type", 0) == 0:
                await walk(m.get("Id"), m.get("Title", ""), m.get("Title", ""))
            else:
                topic(m, "", "")
        counts = ", ".join(f"{v} {k}" for k, v in tree.items() if k not in ("topics", "modules") and v)
        detail = f"{tree['topics']} topics in {tree['modules']} modules" + (f" ({counts})" if counts else "")
        if bad_modules:
            detail += "; unreadable: " + ", ".join(bad_modules)
        status = f"UNKNOWN ({len(bad_modules)} modules unreadable)" if bad_modules else _status(200, tree["topics"])
        r.sources.append(Source("content tree", status, detail))

    if html_topics:
        read = failed = 0
        for topic_id, where, page_url in html_topics[:MAX_HTML_TOPICS]:
            status, html = await api.get_text(f"{le}/content/topics/{topic_id}/file")
            if status == 200:
                read += 1
                scan_html(where, html, urlsplit(page_url).path or "/")
            else:
                failed += 1
                r.unreadable_topics.append(f"{where} (HTTP {status})")
        skipped = max(0, len(html_topics) - MAX_HTML_TOPICS)
        status = "ok" if not failed and not skipped else f"UNKNOWN ({failed} unreadable, {skipped} skipped)"
        r.sources.append(Source("HTML pages in content", status,
                                f"{read} pages scanned for links and AI mentions"))

    # Announcements
    code, news = await get_list(api, f"{le}/news/")
    for item in news:
        where = f"announcement '{item.get('Title', '')}'"
        scan_html(where, (item.get("Body") or {}).get("Html", ""))
        for att in item.get("Attachments") or []:
            add_doc(where, att.get("FileName", ""), size=att.get("Size"),
                    ref=f"download_announcement_file news_id={item.get('Id')} file_id={att.get('FileId')}",
                    key=f"news:{item.get('Id')}:{att.get('FileId')}")
    r.sources.append(Source("announcements", _status(code, news), f"{len(news)} items"))

    # Dropbox folders
    code, folders = await get_list(api, f"{le}/dropbox/folders/")
    for f in folders:
        name = f.get("Name", "").strip()
        where = f"dropbox '{name}'"
        scan_html(where, rich_html(f.get("CustomInstructions") or f.get("Instructions")))
        for att in f.get("Attachments") or []:
            add_doc(where, att.get("FileName", ""), context=name, size=att.get("Size"),
                    ref=f"download_assignment_file folder_id={f.get('Id')} file_id={att.get('FileId')}",
                    key=f"dropbox:{f.get('Id')}:{att.get('FileId')}")
        due, end = f.get("DueDate"), (f.get("Availability") or {}).get("EndDate")
        if due or end:
            r.due.append((due or end, to_local(due), to_local(end), "dropbox", name))
    r.sources.append(Source("dropbox folders", _status(code, folders), f"{len(folders)} folders"))

    # Quizzes
    code, quizzes = await get_list(api, f"{le}/quizzes/")
    for qz in quizzes:
        name = qz.get("Name", "").strip()
        due, end = qz.get("DueDate"), qz.get("EndDate")
        r.due.append((due or end or "", to_local(due), to_local(end), "quiz", name))
        scan_html(f"quiz '{name}'", rich_html(qz.get("Description")))
    r.sources.append(Source("quizzes", _status(code, quizzes), f"{len(quizzes)} quizzes"))

    # Gradebook. Weights only mean something in a Weighted gradebook. A Formula
    # gradebook reports a default 10.0 on every item, and printing that as a weight
    # passed a meaningless number off as the course's grading scheme.
    codes = {}
    codes["setup"], setup = await api.get_status_json(f"{le}/grades/setup/")
    codes["categories"], cats = await api.get_status_json(f"{le}/grades/categories/")
    codes["items"], items = await api.get_status_json(f"{le}/grades/")
    failed = {k: c for k, c in codes.items() if c != 200}
    if not failed:
        system = (setup or {}).get("GradingSystem") or "unknown"
        weighted = system == "Weighted"
        r.grading.append(f"Grading system: {system}")
        if not weighted:
            r.grading.append(f"Weights are not exposed by a {system} gradebook (the API reports a "
                             "placeholder). **Read the outline for the grade breakdown.**")
        in_cat = set()
        for c in cats or []:
            grades = c.get("Grades") or []
            in_cat.update(g.get("Id") for g in grades)
            weight = f"weight {c.get('Weight')}, " if weighted else ""
            r.grading.append(f"{c.get('Name')}: {weight}{len(grades)} items"
                             + (f", drop lowest {c['NumberOfLowestToDrop']}" if c.get("NumberOfLowestToDrop") else ""))
        loose = [i for i in items or [] if i.get("Id") not in in_cat and i.get("GradeType") != "Category"]
        for i in loose:
            weight = f"weight {i.get('Weight')}, " if weighted else ""
            kind = f" [{i['GradeType']}]" if i.get("GradeType") not in (None, "Numeric") else ""
            out_of = f"out of {i['MaxPoints']}" if i.get("MaxPoints") is not None else "no max points"
            r.grading.append(f"{i.get('Name')}{kind} (no category): {weight}{out_of}")
        r.sources.append(Source("gradebook", _status(200, (cats or []) + (items or [])),
                                f"{system}, {len(cats or [])} categories, {len(items or [])} items"))
    else:
        r.sources.append(Source("gradebook", "UNKNOWN (" + ", ".join(
            f"{k} HTTP {c}" for k, c in failed.items()) + ")"))

    # Calendar: events with no matching quiz/dropbox are usually release times.
    code, events = await get_list(api, f"{le}/calendar/events/")
    due_titles = {norm_title(name) for *_, name in r.due}
    for e in events:
        title, start = e.get("Title", ""), e.get("StartDateTime") or ""
        if norm_title(title) not in due_titles:
            r.calendar_only.append((start, to_local(start), title))
    r.sources.append(Source("calendar", _status(code, events), f"{len(events)} events"))

    # Discussions and checklists: counted only, so an empty result is on record.
    for name, path in (("discussions", "discussions/forums/"), ("checklists", "checklists/")):
        code, objs = await get_list(api, f"{le}/{path}")
        r.sources.append(Source(name, _status(code, objs), f"{len(objs)} items"))

    # Classlist: recognised staff roles ONLY. The raw list holds every classmate's
    # name and email, which must never leave this function.
    code, people = await get_list(api, f"{le}/classlist/")
    dropped: set[str] = set()
    for p in people:
        role = p.get("ClasslistRoleDisplayName") or ""
        if STAFF_ROLE_RE.search(role):
            r.staff.append(f"{p.get('DisplayName', '')} ({role})")
        elif role and not re.search(r"student|learner", role, re.IGNORECASE):
            dropped.add(role)
    detail = f"{len(r.staff)} staff" + (f"; roles not reported: {', '.join(sorted(dropped))}" if dropped else "")
    r.sources.append(Source("classlist", _status(code, r.staff), detail))

    # Course home: navbar tools, and document links in homepage widgets.
    code, home = await api.get_text(f"/d2l/home/{course_id}")
    if code == 200:
        nav = parse_nav(home)
        r.nav_unaudited = [n for n, _ in nav if n.lower() not in COVERED_NAV]
        widget_links = 0
        for href, label in parse_html(home)[0]:
            full = urljoin(f"{api.base_url}/d2l/home/{course_id}", href or "")
            if href and (is_document_link(full) or is_outline(label)):
                widget_links += 1
                classify_link("course home page", label, full)
        r.sources.append(Source("course home page", "ok" if nav else "UNKNOWN (navbar not parsed)",
                                f"{len(nav)} navbar tools, {widget_links} document links"))
    else:
        r.sources.append(Source("course home page", f"UNKNOWN (HTTP {code})"))

    # Links to content topics this walk already covers are not "unfollowed".
    walked = {str(tid) for tid, *_ in html_topics} | {
        m.group(1) for d in r.docs if (m := re.search(r"topic_id=(\d+)", d.ref))}
    r.internal_links = [
        (w, t, p) for w, t, p in r.internal_links
        if not ((m := re.search(r"/viewContent/(\d+)/", p)) and m.group(1) in walked)
    ]

    resolve_local(r.docs, local)
    r.missing_docs = [d for d in r.docs if d.local is False]
    r.revised = find_revised(r.docs, local)
    return r
