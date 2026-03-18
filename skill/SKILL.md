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

Result: flat list of all downloadable files with their module context.

### Phase 3: Diff with Local Files

1. Scan the user's local project directory recursively using Glob:
   - `{root}/**/*.pptx`, `{root}/**/*.pdf`, `{root}/**/*.java`, `{root}/**/*.zip`, `{root}/**/*.docx`, etc.
   - Cover all subdirectories: lectures/, labs/, assignments/, docs/, etc.

2. Compare remote files with local files by **filename** (case-insensitive):
   - **NEW** = remote filename not found anywhere in local tree
   - **EXISTS** = filename already present locally

3. Display diff as a table:

```
## Sync Report: {Course Name}

| Status | Module | File | Local Path |
|--------|--------|------|------------|
| NEW    | Week05 | Lecture_5_Slides.pptx | — |
| EXISTS | Week01 | Lecture_1_Slides.pptx | lectures/ |

**{X} new files** to download, {Y} already synced.
```

4. If no new files: report "All files are up to date!" and stop.

### Phase 4: Download + Route

For each NEW file, determine the target directory using routing rules.

#### Default Routing Rules

These rules work for typical course folder structures. Adapt based on the user's actual directory layout.

| Pattern | Target Directory |
|---------|-----------------|
| `*Slides*.pptx` or `*.pptx` | `{root}/lectures/` |
| `*Slides*.pdf` | `{root}/lectures-pdf/` |
| `Assignment*.pdf` | `{root}/assignments/` (or subfolder if numbered) |
| `Lab*.pdf` | `{root}/labs/` |
| `*DemoCode*.zip` or `*.java` | `{root}/demo-code/` |
| `*Practice*.docx` or exam-related | `{root}/exam-prep/` |
| Other | Ask user for target, or save to `{root}/downloads/` |

**Adapt these rules** to match the user's actual directory structure. If unsure, ask before downloading.

#### Download Execution

For each file to download:
1. Create target directory if it doesn't exist (`mkdir -p`)
2. Call `mcp__brightspace__download_file(course_id, topic_id, save_dir=<target>)`
3. Log: "Downloaded: {filename} -> {target_dir} ({size})"
4. If download fails, log error and continue with remaining files

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

Downloaded {N} new files:
| File | Size | Saved To |
|------|------|----------|
| Lecture_5.pptx | 1.3 MB | lectures/ |
| Lab_05.pdf | 245 KB | labs/ |

PDF conversions: {M} files
Errors: {E} (details if any)
```

## Important Notes

- **Idempotent**: Running sync twice won't re-download existing files
- **Non-destructive**: Never deletes local files, only adds new ones
- **Auth required**: If session expired, call `mcp__brightspace__login` first
- **Multi-course**: Can sync all enrolled courses or a specific one
- **Filename collision**: The MCP download tool auto-appends numeric suffixes for duplicates

## Error Handling

| Error | Action |
|-------|--------|
| "Not authenticated" / "Session expired" | Call `mcp__brightspace__login`, then retry |
| Download fails for specific file | Log warning, continue with remaining files |
| LibreOffice not found | Skip PDF conversion, warn user |
| Unknown file type / no routing rule | Ask user where to save, or use `downloads/` |
| Target directory doesn't exist | Create it automatically |
