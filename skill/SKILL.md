---
name: brightspace-sync
description: >
  Sync Brightspace LMS course materials to local project folders.
  Compares remote content with local files, downloads new materials,
  and routes them to the correct directories (lectures/, labs/, assignments/).
  Trigger: "sync", "brightspace sync", "sync courses", "download materials",
  "/brightspace-sync", "/sync"
---

# Brightspace Sync

Sync course materials from D2L Brightspace to local project folders.

## When to Use

- Check for new lecture slides, labs, or assignments
- Bulk-download and organize course materials
- Triggers: "sync", "brightspace sync", "download materials", "/sync"

## Prerequisites

- **Brightspace MCP server** must be running and authenticated
- If tools return "Not authenticated", call `mcp__brightspace__login` first

## Configuration

Before first use, the user must provide:

1. **Course mapping** — which courses to sync and where to save files locally.
   Ask the user: "Which courses do you want to sync, and where are their local folders?"

   Example mapping:
   ```
   CS-101  →  course_id=12345  →  /home/user/courses/CS101
   MATH-200  →  course_id=67890  →  /home/user/courses/MATH200
   ```

2. **Routing rules** — how to sort downloaded files into subdirectories.
   Default rules work for most setups (see Phase 4), but ask the user to confirm.

If the user has used this skill before, check memory for saved course mappings.

## MCP Tools Reference

| Tool | Purpose |
|------|---------|
| `get_courses()` | List active courses |
| `get_course_content(course_id, module_id?)` | List modules/topics in course content |
| `download_file(course_id, topic_id, save_dir)` | Download a content topic file |
| `get_assignments(course_id)` | List all dropbox folders (assignments AND labs) |
| `get_assignment_attachments(course_id, folder_id)` | List files attached to a dropbox folder |
| `download_assignment_file(course_id, folder_id, file_id, save_dir)` | Download a specific dropbox attachment |
| `get_grades(course_id)` | Get grade info |
| `login()` | Authenticate via SSO |

## Workflow

### Phase 1: Discover

1. Call `mcp__brightspace__get_courses` to list enrolled courses
2. Present the course list to the user and confirm which to sync
3. For each target course, call `mcp__brightspace__get_course_content(course_id)` to get root modules
4. Report: "Found {N} modules for {course_name}"

### Phase 2: Scan Remote Files

For each module returned in Phase 1:

1. Call `mcp__brightspace__get_course_content(course_id, module_id=<id>)` to get children
2. Collect **file-type topics only** (`topic_type == "file"`)
3. Record: `{module_title, topic_id, filename, url}`
4. If a child has `has_children == true`, recurse into it (sub-module)
5. Skip items where `is_hidden == true`

**Optimization**: Scan multiple modules in parallel when there are many.

Result: flat list of all downloadable content files with their module context.

### Phase 2B: Scan Assignment/Lab Attachments

1. Call `mcp__brightspace__get_assignments(course_id)` to get all dropbox folders (returns assignments AND labs)
2. For each dropbox folder (skip if `is_hidden`):
   - Call `mcp__brightspace__get_assignment_attachments(course_id, folder_id)` to get attached files
   - Record: `{source: "dropbox", folder_name, folder_id, file_id, filename, size_bytes}`
3. Merge into the same flat file list from Phase 2 (each entry tagged with `source: "content"` or `source: "dropbox"`)

Result: unified flat list of all remote files (content + dropbox) with source and context.

### Phase 3: Diff with Local Files

1. Scan the user's local project directory recursively using Glob:
   - `{root}/lectures/**/*`, `{root}/lectures-pdf/**/*`, `{root}/labs/**/*`
   - `{root}/assignments/**/*`, `{root}/demo-code/**/*`, `{root}/docs/**/*`

2. Compare remote files with local files by **filename** (case-insensitive):
   - **NEW** = remote filename not found anywhere in local tree
   - **EXISTS** = filename already present locally
   - For **dropbox-sourced** files, use **folder_name + filename** for comparison (different assignments can have the same filename like `Tester.java`)

3. Display diff as a table (show folder name in Module column for dropbox-sourced files):

```
## Sync Report: {Course Name}

| Status | Module | File | Local Path |
|--------|--------|------|------------|
| NEW    | Week05 | Lecture_5_Slides.pptx | — |
| NEW    | Assignment 3 | Instructions.pdf | — |
| EXISTS | Week01 | Lecture_1_Slides.pptx | lectures/ |

**{X} new files** to download, {Y} already synced.
```

4. If no new files: report "All files are up to date!" and stop.

### Phase 4: Download + Route

For each NEW file, determine the target directory using routing rules.

#### Default Routing Rules

These rules work for typical course folder structures. Adapt based on the user's actual directory layout.

| Pattern | Source | Target Directory |
|---------|--------|-----------------|
| `*Slides*.pptx` or `*.pptx` | content | `{root}/lectures/` |
| `*Slides*.pdf` | content | `{root}/lectures-pdf/` |
| `*DemoCode*.zip` or `*.java` (from Week modules) | content | `{root}/demo-code/` (+ unzip for zips) |
| `*Practice*.docx` or exam-related | content | `{root}/exam-prep/` |
| Any file from "Assignment*" dropbox folder | dropbox | `{root}/assignments/{normalized_name}/` |
| Any file from "Lab*" dropbox folder | dropbox | `{root}/labs/` |
| Other | -- | Ask user for target, or save to `{root}/downloads/` |

**Adapt these rules** to match the user's actual directory structure. If unsure, ask before downloading.

#### Folder Name Normalization

Before routing, normalize Brightspace dropbox folder names to match local convention:
- Strip leading zeros from numbers: `"Assignment 01"` → `"Assignment 1"`, `"Lab 03"` → `"Lab 3"`
- Logic: replace each numeric group with its integer value (e.g., `01` → `1`)
- Apply this normalization in **both Phase 3 (Diff)** and **Phase 4 (Route)** to ensure existing local folders are matched correctly

#### Download Execution — Content Files

For each content-sourced NEW file:
1. Create target directory if it doesn't exist (`mkdir -p`)
2. Call `mcp__brightspace__download_file(course_id, topic_id, save_dir=<target>)`
3. Log: "Downloaded: {filename} -> {target_dir} ({size})"
4. If download fails, log error and continue with remaining files

#### Zip Auto-Extraction

After downloading zip files to `demo-code/`:

```bash
unzip -o -d {root}/demo-code/ {root}/demo-code/<new_file>.zip
```

Remove the zip after successful extraction if desired (or keep for reference).

#### Download Execution — Dropbox Files

For each dropbox-sourced NEW file:
1. Normalize folder name (strip leading zeros)
2. Create target directory if it doesn't exist (`mkdir -p`)
3. Call `mcp__brightspace__download_assignment_file(course_id, folder_id, file_id, save_dir=<target>)`
4. Log: "Downloaded: {filename} -> {target_dir} ({size})"
5. If download fails, log error and continue with next file

#### PPTX to PDF Conversion (Optional)

If the user maintains a separate PDF directory for slides:

```bash
libreoffice --headless --convert-to pdf --outdir {root}/lectures-pdf/ {root}/lectures/<new_file>.pptx
```

Also convert any existing PPTX files that lack a corresponding PDF.
Skip this step if LibreOffice is not installed (warn the user).

### Phase 5: Report

Display final summary:

```
## Sync Complete: {Course Name}

### Course Content: {N} new files
| File | Size | Saved To |
|------|------|----------|
| Lecture_5.pptx | 1.3 MB | lectures/ |

### Assignment/Lab Attachments: {M} new files
| Folder | File | Size | Saved To |
|--------|------|------|----------|
| Assignment 3 | Instructions.pdf | 263 KB | assignments/Assignment 3/ |
| Lab 05 | Lab05.pdf | 137 KB | labs/ |

PDF conversions: {P} files
Zip extractions: {Z} files
Errors: {E} (details if any)
```

## Important Notes

- **Idempotent**: Running sync twice won't re-download existing files
- **Non-destructive**: Never deletes local files, only adds new ones
- **Auth required**: If session expired, call `mcp__brightspace__login` first
- **Multi-course**: Can sync all enrolled courses or a specific one
- **Filename collision**: The MCP download tools auto-append numeric suffixes for duplicates
- **Parallelism**: Phase 2 module scans can be parallelized with multiple Agent calls
- **Hidden files**: Skip items where `is_hidden == true`
- **Dropbox folders**: Assignments and labs are dropbox folders. Use `get_assignments` to list them, `get_assignment_attachments` to get attached files.
- **Lab vs Assignment**: Inferred from folder name (starts with "Lab" → `labs/`, "Assignment" → `assignments/{folder_name}/`)

## Error Handling

| Error | Action |
|-------|--------|
| "Not authenticated" / "Session expired" | Call `mcp__brightspace__login`, then retry |
| Download fails for specific file | Log warning, continue with remaining files |
| LibreOffice not found | Skip PDF conversion, warn user |
| Unknown file type / no routing rule | Ask user where to save, or use `downloads/` |
| Target directory doesn't exist | Create it automatically |
