---
name: brightspace-sync
description: >
  Sync Brightspace LMS course materials to local project folders.
  Audits every course source first, compares remote content with local files,
  downloads new or revised materials, and routes them to the configured
  directories (lectures/, labs/, assignments/, docs/, ...).
  Trigger: "sync", "brightspace sync", "sync courses", "download materials",
  "check for new materials", "/brightspace-sync", "/sync"
---

# Brightspace Sync

Sync course materials from D2L Brightspace to local project folders, and prove the sync
complete with `audit_course`.

## When to Use

- Check for new lecture slides, labs, assignments or announcements
- Bulk-download and organize course materials
- Find out what a course has posted before recording "not posted" anywhere
- Triggers: "sync", "brightspace sync", "download materials", "/sync"

## Prerequisites

- The **Brightspace MCP server** is running and authenticated
- If a tool returns "Not authenticated" or "Session expired", call `mcp__brightspace__login`
  first. That refresh is silent (persistent browser profile, no stored password, no MFA). It
  only needs the user when the profile's own SSO session has died

## Configuration

Before first use, get from the user and save to memory:

1. **Course mapping**: which courses to sync and their local folders.
   Ask: "Which courses do you want to sync, and where are their local folders?"

   ```
   CS-101    ->  course_id=12345  ->  ~/courses/CS101
   MATH-200  ->  course_id=67890  ->  ~/courses/MATH200
   ```

   Course IDs change every term. Check `get_courses` at the start of a term and retire the
   old IDs instead of syncing them.

2. **Routing rules**: how to sort downloads into subdirectories. The defaults in Phase 4 fit
   most courses; confirm them per course and record any course-specific rule.

3. **Frozen trees** (optional): a retaken course keeps its previous attempt in a term-named
   directory such as `_spring2026/` or `_handout/spring2026/`. Never sync into it and never
   diff against it: a current-term file whose name matches an old one must still download.
   `audit_course` skips term-named directories on its own.

If the user has used this skill before, read the saved mapping from memory.

## A sync is not a course audit: run `audit_course` every time

A complete download is not a complete picture of the course. A plain sync reads content
topics and dropbox attachments. It misses, for example:

1. An outline linked from inside an HTML page, not posted as a topic
2. Outlines attached to the Content tool's **Overview**, which is not a topic either
3. Staff listed only in the classlist, gradebook weights, quiz due times, announcements,
   and why some topics return 404 (release-scheduled content)
4. Handout PDFs linked *from inside* a content page. The sync downloads the page and never
   follows its links

The cause is always the same: a source the sync does not read gets reported as "nothing
there". **`mcp__brightspace__audit_course` exists so that cannot happen.**

### Phase 0: Audit (mandatory, before and after the sync)

For each target course call:

```
mcp__brightspace__audit_course(course_id=<id>, local_root="<course folder>")
```

It reads the Overview, the whole content tree and every HTML page in it (links resolved
against their page), announcements, dropbox folders, quizzes, the gradebook setup, the course
calendar, discussions, checklists, the classlist (staff only) and the course home navbar. The
report gives a status per source.

Rules for reading the report:

- **UNKNOWN is not empty.** A source marked UNKNOWN could not be read. Record nothing about it,
  least of all "not posted". Fix the cause (usually `login`) and re-run.
- **"Navbar tools this audit does not read"** is the residual gap. If any of them could hold
  course information (Links, Surveys, Self Assessments, Groups), open the course in a browser.
- **Course outline candidates:** download every one not present locally
  (`download_course_overview` for the Overview attachment, `download_file` for a topic,
  `download_linked_file` for a link) into the course's documents folder, then **read it**
  (`pdftotext -layout`). Record the AI policy verbatim, the weights, and any automatic-fail
  rule (attendance, exam average, missed labs). File contents are not scanned by the tool.
- **AI mentions** lists hits in HTML sources only. An empty list says nothing about PDFs.
- **Documents not present locally:** fetch each with the call printed next to it
  (`download_file topic_id=...`, `download_assignment_file folder_id=... file_id=...`,
  `download_announcement_file news_id=... file_id=...`, or `download_linked_file url: ...`).
  External ones (marked `external`) are public pages: fetch them without the session. An entry
  noted "same name in N places" means one local copy was found for several remote files of
  that name: route each into its own folder.
- **Remote files changed after the local copy:** the remote file is newer than every local copy
  of that name (content topics) or a different size (dropbox and announcement attachments).
  Re-download, compare, and replace the local copy if it differs.
- **Closed topics not present locally:** the end date has passed. They answer 403 and cannot be
  downloaded, and they are **not** in the missing count. Do not call `login` for them.
- **Dropbox link attachments** come back from `get_assignment_attachments` with `file_id` 0 and
  a `link_url`. Fetch them with `download_linked_file`.
- **Content not yet released** lists topics with a future start date. Their files 404 until
  then. That is expected. Re-run the audit after the date.
- **Brightspace links not followed** are same-host links that are neither a file nor a walked
  topic, usually `quickLink ... type=content` pointers. Open each in a browser: one may be the
  only route to a page. A candidate marked "open in a browser" is a page, not a file.
- **Ignored by .syncignore** lists files the user deleted on purpose (see Phase 3). They are
  out of the missing count. Never download them.
- **Due items and calendar-only events** are in local time with the zone shown. Brightspace
  stores times in UTC; always read the local value, not the `Z` one.
- **Never write "not posted", "not declared" or "not published"** unless the audit ran with zero
  UNKNOWN sources and the fact is absent from every source it lists. Even then write "not found
  by audit_course on <date>".

## MCP Tools Reference

| Tool | Purpose |
|------|---------|
| `get_courses()` | List active courses |
| `audit_course(course_id, local_root?)` | **Phase 0.** Every source, with status, outline candidates, missing documents |
| `get_course_overview(course_id)` | Content Overview text, links and attachment name |
| `download_course_overview(course_id, save_dir)` | Download the Overview attachment (often the outline) |
| `get_course_content(course_id, module_id?)` | List modules and topics |
| `download_file(course_id, topic_id, save_dir)` | Download a content topic file |
| `download_linked_file(url, save_dir)` | Download a Brightspace file linked from inside a page. Same host only |
| `get_assignments(course_id)` | List all dropbox folders (assignments AND labs) |
| `get_assignment_attachments(course_id, folder_id)` | List files and links attached to a dropbox folder |
| `download_assignment_file(course_id, folder_id, file_id, save_dir)` | Download a dropbox attachment |
| `download_announcement_file(course_id, news_id, file_id, save_dir)` | Download a file attached to an announcement |
| `get_calendar(days?)` | Upcoming calendar events across active courses |
| `get_grades(course_id)` | Your own grade values. `audit_course` prints category weights only for a Weighted gradebook; a Formula gradebook exposes none, so read the outline |
| `login()` | Silent SSO refresh from the persistent browser profile |

If a tool in this table is missing from the tool list, the MCP server is running older code:
reconnect it (`/mcp`).

## Workflow

### Phase 1: Discover

1. Call `get_courses` and pick the target course(s): all mapped courses unless the user names one
2. Call `get_course_content(course_id)` for the root modules
3. Log: "Found {N} modules for {course_name}"

### Phase 2: Scan Remote Files

For each module:

1. Call `get_course_content(course_id, module_id=<id>)` to get its children
2. Collect **file-type topics** (`topic_type == "file"`). Link topics (web textbooks, quizzes)
   are not downloadable: skip them, do not report them as errors
3. Record `{module_title, topic_id, filename, topic_type}`
4. Recurse into sub-modules

### Phase 2B: Scan Dropbox Attachments

1. Call `get_assignments(course_id)` (returns assignments AND labs)
2. For each folder that is not `is_hidden`, call `get_assignment_attachments` and record
   `{source: "dropbox", folder_name, folder_id, file_id, filename, size_bytes, link_url}`
3. Merge into one list tagged `source: "content"` or `source: "dropbox"`

### Phase 3: Diff with Local Files

1. Scan every configured target directory of the course. Exclude frozen term trees
   (Configuration item 3) and the user's own work folders.

2. Compare by **filename** (case-insensitive, extension-aware):
   - **NEW** = remote file not found locally
   - **EXISTS** = filename present in the matching folder
   - For **dropbox** files compare **folder_name + filename**: different assignments can share
     a filename like `Tester.java`. The same holds for content: one local `homework.pdf` does
     not cover a `homework.pdf` in Week 1 **and** Week 2
   - **REVISED** = present, but listed by the audit under "Remote files changed after the local
     copy". Download again (it lands as `X (1).ext`), compare, keep one
   - **HTML is stored as PDF** (Phase 4). A remote `X.html` is **EXISTS** when a local `X.pdf`
     sits in the matching folder. `audit_course` applies the same alias
   - **IGNORED** = listed in `<course folder>/.syncignore`. **The user deleted it on purpose:
     never download it again**, even when a filename-only diff calls it NEW. One filename per
     line, `#` comments, and an `X.pdf` entry also covers a remote `X.html`. When a synced file
     disappears from the tree, assume the user deleted it and add it to `.syncignore`; ask only
     if unclear

3. Show the diff:

```
## Sync Report: CS-101

| Status | Module | File | Local Path |
|--------|--------|------|------------|
| NEW    | Lecture Slides | L6 - Inheritance.pptx | - |
| NEW    | Sample Code | Die.java | - |
| EXISTS | Lecture Slides | L1 - Introduction.pptx | lectures/ |

**{X} new files** to download, {Y} already synced.
```

4. If nothing is new or revised, say so and **skip to Phase 5**. Do not stop here: the audit
   re-run in Phase 5 is what proves the sync complete.

### Phase 4: Download and Route

Default routing rules. Confirm them with the user and adapt per course; module names differ
between instructors.

| Pattern | Source | Target Directory |
|---------|--------|-----------------|
| Course outline (Overview attachment or outline topic) | overview/content | `docs/` |
| `*.pptx`, `*.ppt` from a slides or week module | content | `lectures/` |
| `*.pdf` slides | content | `lectures-pdf/` |
| Sample or demo code (`.java`, `.py`, `.zip`) | content | `demo-code/` (unzip a zip) |
| Any file from a "Lab*" module or dropbox folder | content/dropbox | `labs/<normalized name>/` |
| Any file from an "Assignment*" dropbox folder | dropbox | `assignments/<normalized name>/` |
| Midterm or final review material | content | `exam-prep/` |
| Practice questions | content | `practice/` |
| Other | - | Ask the user |

**Folder name normalization:** strip leading zeros from numbers ("Assignment 01" ->
"Assignment 1", "Lab 03" -> "Lab 3"). Apply it in Phase 3 and Phase 4 alike so existing
folders match.

**Download execution:** for each NEW file call `download_file` (content) or
`download_assignment_file` (dropbox), log "Downloaded: {filename} -> {target} ({size})", and on
a single-file failure log it and continue.

**Zip extraction:** `unzip -o -d <target>/ <target>/<file>.zip`. **Keep the zip**: the audit
compares by filename, so a deleted zip is reported missing on every later run.

**HTML to PDF (recommended):** keep no `.html` in a course tree. Convert each downloaded page
with a headless browser, for example
`google-chrome --headless --print-to-pdf="<target>/X.pdf" "<target>/X.html"`, and delete the
`.html` only after the PDF exists. Brightspace-hosted images and CSS need the session, so they
render as alt text or unstyled; the text is what is kept. When done,
`find <course folder> -name '*.htm*'` should return nothing outside frozen trees.

**PPTX to PDF (optional):**
`libreoffice --headless --convert-to pdf --outdir <course>/lectures-pdf/ <course>/lectures/<file>.pptx`.
On Fedora, `Error: source file could not be loaded` on a valid `.pptx` means the
`libreoffice-impress` package is missing, not a corrupt file.

### Phase 5: Report

```
## Sync Complete

### Course Content: {N} new files
| File | Size | Saved To |
|------|------|----------|
| L6 - Inheritance.pptx | 1.3 MB | lectures/ |

### Dropbox Attachments: {M} new files
| Folder | File | Size | Saved To |
|--------|------|------|----------|
| Assignment 4 | Starter.java | 3.2 KB | assignments/Assignment 4/ |

Conversions: {P} PPTX -> PDF, {H} HTML -> PDF
Zip extractions: {Z}
Errors: {E} (list them)

### Audit (re-run after downloading)
| Course | UNKNOWN sources | Outline read | Missing documents | Unaudited navbar tools |
|--------|-----------------|--------------|-------------------|------------------------|
| CS-101 | 0 | yes | 0 | Links, Surveys |
```

Re-run `audit_course` after the downloads. The sync is done only when every course shows
**0 UNKNOWN** and **0 missing documents**, and every outline candidate has been read. Report
anything still open instead of calling the sync complete.

## Important Notes

- **Idempotent**: running twice does not re-download existing files
- **Non-destructive**: never deletes local files, only adds. The exception is an `.html` source
  removed after its PDF was written
- **User deletions are final**: a file the user removed never comes back. Record it in
  `.syncignore`
- **No overwrites**: the download tools never overwrite. A second copy lands as `X (1).ext` and
  the returned `filename` is that real name. Compare the two and keep one
- **Parallelism**: Phase 2 module scans can run in parallel sub-agents for large courses

## Error Handling

| Error | Action |
|-------|--------|
| "Not authenticated" / "Session expired" | Call `login`, then retry |
| HTTP 401 or 403, or several "Error fetching" / "Download failed" in a row | Treat as an auth failure, not missing data: call `login` and retry. D2L answers an expired session with 403. Never record anything read in that state as empty. A 403 on a topic listed under "Closed topics" is expected |
| `login` returns bootstrap instructions | The browser profile's SSO session is gone. The user runs `brightspace-mcp-login` in a desktop session (it needs a human for MFA) |
| 404 on one file | Log and continue. Expected for topics under "Content not yet released" |
| LibreOffice or Chrome not found | Skip that conversion, warn the user |
| Target directory missing | Create it with `mkdir -p` |
