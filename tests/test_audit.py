"""Tests for the course audit.

The audit exists because content syncs kept reporting "not posted" for things that
were posted somewhere the sync never looked. These tests pin the properties that
matter: every source gets a status, an unreadable source is UNKNOWN rather than
empty, outlines are found wherever they hide, and classmates never leak out of the
classlist.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from brightspace_mcp.api import BrightspaceAPI
from brightspace_mcp.audit import (
    ai_mentions,
    audit_course,
    is_document_link,
    is_outline,
    local_index,
    parse_html,
    parse_nav,
    render,
    rich_html,
    to_local,
    url_filename,
)

BASE = "https://d2l.example.ca"
OU = 1234
LE = f"/d2l/api/le/1.92/{OU}"


# --- pure helpers -----------------------------------------------------------


def test_parse_html_links_and_text():
    links, text = parse_html('<p>See the <a href="/x/Outline.pdf?ou=1">course outline here.</a></p>')
    assert links == [("/x/Outline.pdf?ou=1", "course outline here.")]
    assert text == "See the course outline here."


def test_url_filename_strips_query_and_decodes():
    assert url_filename("/content/enforced/1-X/Course%20Outline.pdf?_&d2lSessionVal=abc") == "Course Outline.pdf"


@pytest.mark.parametrize(
    "href, expected",
    [
        ("/content/enforced/1234-TEST/labs/lab1_intro.pdf", True),
        ("https://host/files/outline.DOCX", True),
        ("/d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&type=coursefile&fileId=9", True),
        ("https://openstax.org/books/physics", False),
        ("/d2l/le/content/1/Home", False),
    ],
)
def test_is_document_link(href, expected):
    assert is_document_link(href) is expected


def test_is_outline():
    assert is_outline("TEST_1000_Syllabus.pdf")
    assert is_outline("x.pdf", "course outline here.")
    assert not is_outline("Lab 1.pdf", "handout")


def test_ai_mentions_acronym_is_case_sensitive():
    text = "Do not use AI software. We said to maintain the trail. ChatGPT is also out."
    hits = ai_mentions(text, width=10)
    assert len(hits) == 2
    assert "AI software" in hits[0]
    assert "ChatGPT" in hits[1]
    assert ai_mentions("the rain in spain said nothing") == []


def test_local_filenames_skips_frozen_and_hidden_dirs(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "Outline.PDF").write_text("x")
    # Frozen previous term, in the old layout and the current one.
    (tmp_path / "_spring2026").mkdir()
    (tmp_path / "_spring2026" / "old.pdf").write_text("x")
    (tmp_path / "_handout" / "spring2026").mkdir(parents=True)
    (tmp_path / "_handout" / "spring2026" / "older.pdf").write_text("x")
    (tmp_path / "_scratch").mkdir()
    (tmp_path / "_scratch" / "tmp.pdf").write_text("x")
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    assert set(local_index(tmp_path)) == {"outline.pdf", "outline.html", "outline.htm"}


def test_local_index_walks_current_term_handouts(tmp_path: Path):
    # _handout/ holds this term's handouts. Skipping every `_*` dir reported them missing.
    (tmp_path / "_handout" / "docs").mkdir(parents=True)
    (tmp_path / "_handout" / "docs" / "x.pdf").write_text("x")
    (tmp_path / "_handout" / "demo-code").mkdir()
    (tmp_path / "_handout" / "demo-code" / "Y.java").write_text("x")
    (tmp_path / "_handout" / "fall2025").mkdir()
    (tmp_path / "_handout" / "fall2025" / "z.pdf").write_text("x")
    index = local_index(tmp_path)
    assert {"x.pdf", "y.java"} <= set(index) and "z.pdf" not in index
    assert index["y.java"][0].parts == (("handout",), ("demo", "code"))
    assert local_index(tmp_path / "missing") is None
    assert local_index(None) is None


def test_to_local_uses_wall_clock_not_iso_date():
    # 23:59 Pacific daylight time is 06:59Z the NEXT day. Slicing the ISO string was
    # the original bug and gave the wrong day.
    assert to_local("2026-09-22T06:59:59.000Z") == "Mon 2026-09-21 23:59 PDT"
    assert to_local(None) == "-"


_BC_PERMANENT = (
    datetime(2026, 11, 3, 8, tzinfo=UTC).astimezone(ZoneInfo("America/Vancouver")).utcoffset().total_seconds()
    == -7 * 3600
)


@pytest.mark.skipif(not _BC_PERMANENT, reason="system tzdata older than 2026c (BC permanent UTC-7)")
def test_to_local_follows_bc_permanent_utc_minus_7():
    # tzdata 2026c: BC stays on UTC-7 after 2026-11-01. A date an instructor entered
    # as 23:59 under the old PST rule (07:59Z) is 00:59 on the real clock.
    assert to_local("2026-11-03T07:59:59.000Z") == "Tue 2026-11-03 00:59 MST"


def test_parse_nav_dedupes_and_strips_query():
    html = (
        '<d2l-menu-item-link text="Content" href="/d2l/le/content/1/Home"></d2l-menu-item-link>'
        '<d2l-menu-item-link href="/d2l/lms/links/view_links.d2l?ou=1" text="Links"></d2l-menu-item-link>'
        '<d2l-menu-item-link text="Content" href="/d2l/le/content/1/Home"></d2l-menu-item-link>'
    )
    assert parse_nav(html) == [("Content", "/d2l/le/content/1/Home"), ("Links", "/d2l/lms/links/view_links.d2l")]


def test_rich_html_handles_nesting():
    assert rich_html({"Text": {"Text": "t", "Html": "<b>x</b>"}}) == "<b>x</b>"
    assert rich_html({"Html": "<i>y</i>"}) == "<i>y</i>"
    assert rich_html(None) == ""


# --- audit against a fake API ----------------------------------------------


class _Cfg:
    le_version = "1.92"


class FakeAPI:
    """Just the surface audit_course touches. Unlisted paths are 404."""

    def __init__(self, json_routes: dict, text_routes: dict | None = None, attachment=(404, "", 0)):
        self.config = _Cfg()
        self.base_url = BASE
        self.json_routes = json_routes
        self.text_routes = text_routes or {}
        self.attachment = attachment

    async def get_status_json(self, path):
        return self.json_routes.get(path, (404, None))

    async def get_text(self, path):
        return self.text_routes.get(path, (404, ""))

    async def overview_attachment_info(self, course_id):
        return self.attachment


NAV = (
    '<d2l-menu-item-link text="Content" href="/c"></d2l-menu-item-link>'
    '<d2l-menu-item-link text="Links" href="/l"></d2l-menu-item-link>'
    '<d2l-menu-item-link text="Surveys" href="/s"></d2l-menu-item-link>'
)


def _topic(tid, title, url, kind=1, **kw):
    """A topic as modules/{id}/structure/ returns it. kind 1 = file, 3 = link."""
    return {"Type": 1, "Id": tid, "Title": title, "TopicType": kind, "Url": url, **kw}


STRUCT = f"{LE}/content/modules/1/structure/"
NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)


def _full_course():
    stub = ('<p>Find the <a href="/content/enforced/1234-X/TEST_Outline_Roe.pdf">course outline here.</a> '
            'Do not use AI software on anything you hand in.</p>')
    return FakeAPI(
        json_routes={
            f"{LE}/overview": (200, {"Description": {"Text": "", "Html": ""}, "HasAttachment": True}),
            f"{LE}/content/root/": (200, [{"Type": 0, "Id": 1, "Title": "Course Outline and textbook",
                                           "Description": {"Html": "<p>Generative AI is not allowed.</p>"}}]),
            STRUCT: (200, [
                _topic(11, "Course Outline", "/content/enforced/1234-X/Course%20Outline5.html",
                       Description={"Html": '<a href="/content/enforced/1234-X/rubric.pdf">rubric</a>'}),
                _topic(12, "Web textbook", "https://openstax.org/x", kind=3),
            ]),
            f"{LE}/news/": (200, [{"Title": "Welcome", "Body": {"Html": "<p>hi</p>"}, "Attachments": []}]),
            f"{LE}/dropbox/folders/": (200, [{"Name": "Prelab 1", "DueDate": "2026-09-15T06:59:59.000Z",
                                              "Attachments": [{"FileName": "prelab.pdf"}]}]),
            f"{LE}/quizzes/": (200, {"Objects": [{"Name": "Week 1 Quiz", "DueDate": "2026-09-10T17:00:00.000Z",
                                                  "EndDate": "2026-09-10T17:05:00.000Z",
                                                  "Instructions": {"Text": {"Html": "<p>No ChatGPT.</p>"}},
                                                  "Header": {"Html": '<a href="/content/enforced/1234-X/formulas.pdf">formulas</a>'},
                                                  "Footer": {"Html": "<p>Copilot is banned.</p>"}}], "Next": None}),
            f"{LE}/grades/setup/": (200, {"GradingSystem": "Weighted"}),
            f"{LE}/grades/categories/": (200, [{"Name": "Labs", "Weight": 30.0, "Grades": [{"Id": 1}]}]),
            f"{LE}/grades/": (200, [{"Id": 1, "Name": "Lab 1", "Weight": 10}]),
            f"{LE}/calendar/events/": (200, [
                {"Title": "Prelab 1", "StartDateTime": "2026-09-15T06:59:59.000Z"},
                {"Title": "Lab 2: Motion", "StartDateTime": "2026-09-16T00:00:00.000Z"},
            ]),
            f"{LE}/discussions/forums/": (200, []),
            f"{LE}/checklists/": (200, {"Objects": []}),
            f"{LE}/classlist/": (200, [
                {"DisplayName": "Teacher, Terry", "ClasslistRoleDisplayName": "Instructor"},
                {"DisplayName": "Classmate, Casey", "ClasslistRoleDisplayName": "Student",
                 "Email": "casey@example.ca"},
            ]),
        },
        text_routes={
            f"{LE}/content/topics/11/file": (200, stub),
            f"/d2l/home/{OU}": (200, NAV),
        },
        attachment=(200, "TEST_1000_001_Doe_202630.pdf", 125449),
    )


def _run(api, root=None):
    return asyncio.run(audit_course(api, OU, "TEST-1000", root, now=NOW))


def test_audit_finds_outline_everywhere_it_hides(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "Course Outline5.html").write_text("x")
    r = _run(_full_course(), tmp_path)
    where = {d.where: d for d in r.outline}
    # Overview attachment: not a topic, never seen by a content walk.
    assert where["overview attachment"].name == "TEST_1000_001_Doe_202630.pdf"
    assert where["overview attachment"].local is False
    # The HTML stub topic itself, and the PDF linked from inside it.
    assert where["content Course Outline and textbook"].local is True
    linked = where["link in Course Outline and textbook/Course Outline"]
    assert linked.name == "TEST_Outline_Roe.pdf"
    assert linked.href == "/content/enforced/1234-X/TEST_Outline_Roe.pdf"


def test_audit_scans_html_for_ai_policy():
    r = _run(_full_course())
    assert any("AI software" in snip for _, snip in r.ai)
    # Module descriptions and quiz Instructions/Header/Footer are HTML sources too.
    where = {w for w, _ in r.ai}
    assert "content Course Outline and textbook (module description)" in where
    assert "quiz 'Week 1 Quiz' instructions" in where
    assert "quiz 'Week 1 Quiz' footer" in where


def test_audit_follows_links_in_descriptions(tmp_path):
    missing = {d.name: d for d in _run(_full_course(), tmp_path).missing_docs}
    assert missing["rubric.pdf"].where == "link in content Course Outline and textbook/Course Outline (description)"
    assert missing["formulas.pdf"].where == "link in quiz 'Week 1 Quiz' header"


def test_summary_line_counts_unaudited_nav_tools():
    r = _run(_full_course())
    report = render(r)
    assert "**0 UNKNOWN**. **2 navbar tools not read.**" in report
    detail = {s.name: s.detail for s in r.sources}
    assert detail["discussions"] == "0 items, counted only, not read"
    assert detail["checklists"] == "0 items, counted only, not read"


def test_audit_classlist_keeps_staff_only():
    r = _run(_full_course())
    assert r.staff == ["Teacher, Terry (Instructor)"]
    report = render(r)
    assert "Casey" not in report and "casey@example.ca" not in report


def test_audit_due_items_and_calendar_only():
    r = _run(_full_course())
    kinds = {(kind, name) for *_, kind, name in r.due}
    assert kinds == {("dropbox", "Prelab 1"), ("quiz", "Week 1 Quiz")}
    # Calendar event matching a due item is not repeated; the release event is kept.
    assert [t for *_, t in r.calendar_only] == ["Lab 2: Motion"]


def test_audit_lists_unaudited_nav_tools():
    r = _run(_full_course())
    assert r.nav_unaudited == ["Links", "Surveys"]


def test_unreadable_source_is_unknown_not_empty():
    api = _full_course()
    api.json_routes[f"{LE}/news/"] = (403, None)
    api.json_routes[f"{LE}/grades/"] = (500, None)
    r = _run(api)
    status = {s.name: s.status for s in r.sources}
    assert status["announcements"] == "UNKNOWN (HTTP 403)"
    assert status["gradebook"] == "UNKNOWN (items HTTP 500)"
    assert status["discussions"] == "empty"
    report = render(r)
    assert "**2 UNKNOWN**" in report
    assert "UNKNOWN is not empty" in report


def test_unreadable_overview_attachment_is_unknown_and_still_a_candidate():
    # HasAttachment is true but the GET fails. That is "unreadable", not "no outline".
    api = _full_course()
    api.attachment = (403, "", 0)
    r = _run(api)
    ov = next(s for s in r.sources if s.name == "overview")
    assert ov.status == "UNKNOWN (attachment HTTP 403)"
    assert "unreadable" in ov.detail
    assert any(d.where == "overview attachment" for d in r.outline)
    assert "**1 UNKNOWN**" in render(r)


def test_long_overview_text_is_an_outline_candidate():
    # An outline pasted into the Overview body has no file and no link to find.
    api = _full_course()
    api.attachment = (404, "", 0)
    api.json_routes[f"{LE}/overview"] = (200, {
        "Description": {"Text": "Grading: labs 30%. " * 60, "Html": "<p>Grading</p>"},
        "HasAttachment": False})
    r = _run(api)
    assert any(d.where == "overview text" for d in r.outline)
    short = _full_course()
    short.json_routes[f"{LE}/overview"] = (200, {"Description": {"Text": "Welcome!", "Html": ""},
                                                  "HasAttachment": False})
    assert not any(d.where == "overview text" for d in _run(short).outline)


def test_formula_gradebook_prints_no_weights():
    # Seen live: every item reports the placeholder weight 10.0 under a Formula system.
    api = _full_course()
    api.json_routes[f"{LE}/grades/setup/"] = (200, {"GradingSystem": "Formula"})
    api.json_routes[f"{LE}/grades/categories/"] = (200, [{"Name": "Test 1", "Weight": None, "Grades": [{"Id": 1}]}])
    api.json_routes[f"{LE}/grades/"] = (200, [{"Id": 1, "Name": "M1", "Weight": 10.0},
                                              {"Id": 9, "Name": "Tests Total", "GradeType": "Formula",
                                               "Weight": 10.0, "MaxPoints": 100},
                                              {"Id": 10, "Name": "Quiz grade", "GradeType": "Formula"}])
    r = _run(api)
    text = "\n".join(r.grading)
    assert "Grading system: Formula" in text
    assert "weight" not in text.replace("Weights are not exposed", "")
    assert "Read the outline" in text
    assert "Tests Total [Formula] (no category): out of 100" in text
    assert "None" not in text


def test_weighted_gradebook_keeps_weights():
    r = _run(_full_course())
    assert "Labs: weight 30.0, 1 items" in r.grading


def test_missing_local_root_is_unknown_not_zero_missing(tmp_path):
    # A typo ("TEST 1000" for "TEST1000") used to disable the comparison silently and
    # report 0 missing documents.
    r = _run(_full_course(), tmp_path / "TEST 1000")
    status = {s.name: s.status for s in r.sources}
    assert status["local tree"] == "UNKNOWN (path not found)"
    assert "not a directory" in render(r)
    assert status.get("local tree") and _run(_full_course(), tmp_path).sources[0].status == "ok"


def test_no_outline_is_worded_as_not_found_not_absent():
    api = FakeAPI(json_routes={f"{LE}/overview": (404, None)}, text_routes={f"/d2l/home/{OU}": (200, NAV)})
    report = render(_run(api))
    assert "None found" in report
    assert '"not found by this audit", not "not posted"' in report
    assert "content tree" in report.split("Not searchable because unreadable:")[1]


# --- linked-file download guard --------------------------------------------


class _Config:
    brightspace_url = BASE
    le_version = "1.92"


@pytest.mark.parametrize("url", ["https://evil.example.com/x.pdf", "//evil.example.com/x.pdf", "x.pdf"])
def test_download_linked_file_refuses_other_hosts(url, tmp_path):
    api = BrightspaceAPI(_Config(), [])
    with pytest.raises(ValueError, match="not a Brightspace path"):
        asyncio.run(api.download_linked_file(url, tmp_path))
    asyncio.run(api.close())



def test_relative_links_resolve_against_their_page(tmp_path):
    # A lab page links its handouts relatively. The sync downloaded the page and never
    # followed the links, so six PDFs went missing without anything saying so.
    api = _full_course()
    api.json_routes[STRUCT][1].append(_topic(13, "Lab 1", "/content/enforced/1234-X/Materials/Lab1.html"))
    api.text_routes[f"{LE}/content/topics/13/file"] = (200, (
        '<a href="lab1_intro.pdf">handout</a>'
        '<a href="../shared/report.pdf">report template</a>'
        '<a href="https://physics.example.org/extra.pdf">extra</a>'
        f'<a href="{BASE}/content/enforced/1234-X/Materials/../marking.pdf">rubric</a>'))
    missing = {d.name: d for d in _run(api, tmp_path).missing_docs}
    assert missing["lab1_intro.pdf"].href == "/content/enforced/1234-X/Materials/lab1_intro.pdf"
    assert missing["report.pdf"].href == "/content/enforced/1234-X/shared/report.pdf"
    assert missing["extra.pdf"].external is True
    assert missing["extra.pdf"].href == "https://physics.example.org/extra.pdf"
    # Absolute same-host href with a dot segment, as Brightspace's editor writes them.
    assert missing["marking.pdf"].href == "/content/enforced/1234-X/marking.pdf"


# --- findings from the 2026-09-10 review ---------------------------------------


def test_scheduled_topics_are_listed_not_dropped(tmp_path):
    # content/toc dropped Labs 2-9 (future StartDate) and an ended quiz link. The
    # structure walk keeps them; a future topic is "not yet released", not missing.
    api = _full_course()
    api.json_routes[STRUCT][1].extend([
        _topic(20, "Lab 2: Motion", "/content/enforced/1234-X/Lab2.html", StartDate="2026-09-16T00:00:00.000Z"),
        _topic(21, "Week 1 Quiz on the course outline", f"/d2l/common/dialogs/quickLink/quickLink.d2l?ou={OU}&type=quiz&rcode=Q",
               kind=3, EndDate="2026-09-10T17:05:00.000Z"),
    ])
    r = _run(api, tmp_path)
    assert [t for *_, t in r.scheduled] == ["Course Outline and textbook/Lab 2: Motion"]
    assert "Content not yet released" in render(r)
    assert not any(d.name == "Lab2.html" for d in r.missing_docs)
    tree = next(s for s in r.sources if s.name == "content tree")
    assert "1 not yet released" in tree.detail and "1 closed" in tree.detail
    assert not any("quiz" in p for *_, p in r.internal_links)  # the quiz tool covers it
    assert not any("Quiz" in d.name for d in r.outline)


def test_unreadable_module_makes_the_tree_unknown():
    api = _full_course()
    api.json_routes[f"{LE}/content/root/"] = (200, [{"Type": 0, "Id": 1, "Title": "Course Outline and textbook"},
                                                     {"Type": 0, "Id": 2, "Title": "Week 2"}])
    api.json_routes[f"{LE}/content/modules/2/structure/"] = (500, None)
    tree = next(s for s in _run(api).sources if s.name == "content tree")
    assert tree.status == "UNKNOWN (1 modules unreadable)"
    assert "Week 2 (HTTP 500)" in tree.detail


def test_code_files_and_unfollowed_brightspace_links_are_reported(tmp_path):
    api = _full_course()
    api.json_routes[STRUCT][1].extend([
        _topic(13, "Lab 3", "/content/enforced/1234-X/Lab3.html"),
        _topic(14, "Lab Manual", "/content/enforced/1234-X/LabManual.pdf", kind=3),
    ])
    api.text_routes[f"{LE}/content/topics/13/file"] = (200, (
        '<a href="Tester.java">starter</a><a href="data.csv">data</a><a href="solve.py">py</a>'
        f'<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou={OU}&type=content&rcode=X&d2lSessionVal=SECRET">'
        'lab policy</a>'
        f'<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou={OU}&type=content&rcode=X">again</a>'
        f'<a href="/d2l/common/dialogs/quickLink/quickLink.d2l?ou={OU}&type=dropbox&rcode=D">prelab</a>'))
    r = _run(api, tmp_path)
    names = {d.name for d in r.missing_docs}
    assert {"Tester.java", "data.csv", "solve.py", "LabManual.pdf"} <= names
    # A link-type topic pointing at a Brightspace file is a document, not skipped.
    manual = next(d for d in r.missing_docs if d.name == "LabManual.pdf")
    assert manual.href == "/content/enforced/1234-X/LabManual.pdf"
    # A same-host link that is neither a file nor a walked topic is listed, token-free.
    [(_, text, path)] = r.internal_links
    assert text == "lab policy" and "rcode=X" in path and "SECRET" not in render(r)


def test_same_name_in_two_places_needs_two_local_copies(tmp_path):
    # One local homework.pdf used to stand for both weeks' files.
    api = _full_course()
    api.json_routes[f"{LE}/content/root/"][1].append({"Type": 0, "Id": 5, "Title": "Week 1"})
    api.json_routes[f"{LE}/content/root/"][1].append({"Type": 0, "Id": 6, "Title": "Week 2"})
    api.json_routes[f"{LE}/content/modules/5/structure/"] = (200, [_topic(51, "HW", "/c/W1/homework.pdf")])
    api.json_routes[f"{LE}/content/modules/6/structure/"] = (200, [_topic(61, "HW", "/c/W2/homework.pdf")])
    api.json_routes[f"{LE}/dropbox/folders/"] = (200, [
        {"Id": 7, "Name": "Assignment 01", "Attachments": [{"FileId": 70, "FileName": "Tester.java", "Size": 5}]},
        {"Id": 8, "Name": "Assignment 02", "Attachments": [{"FileId": 80, "FileName": "Tester.java", "Size": 5}]},
    ])
    (tmp_path / "lectures" / "week01").mkdir(parents=True)
    (tmp_path / "lectures" / "week01" / "homework.pdf").write_text("x")
    (tmp_path / "assignments" / "Assignment 1").mkdir(parents=True)
    (tmp_path / "assignments" / "Assignment 1" / "Tester.java").write_text("12345")
    r = _run(api, tmp_path)
    missing = {(d.where, d.name) for d in r.missing_docs}
    assert ("content Week 2", "homework.pdf") in missing
    assert ("content Week 1", "homework.pdf") not in missing
    assert ("dropbox 'Assignment 02'", "Tester.java") in missing
    assert ("dropbox 'Assignment 01'", "Tester.java") not in missing
    # Every missing entry says how to fetch it.
    report = render(r)
    assert "`download_file topic_id=61`" in report
    assert "`download_assignment_file folder_id=8 file_id=80`" in report


def test_one_file_linked_twice_counts_once(tmp_path):
    api = _full_course()
    api.text_routes[f"{LE}/content/topics/11/file"] = (200, (
        '<a href="/content/enforced/1234-X/TEST_Outline_Roe.pdf">outline</a>'
        '<a href="/content/enforced/1234-X/TEST_Outline_Roe.pdf">course outline here.</a>'))
    api.json_routes[f"{LE}/overview"] = (200, {"HasAttachment": False, "Description": {
        "Text": "", "Html": '<a href="/content/enforced/1234-X/TEST_Outline_Roe.pdf">outline</a>'}})
    (tmp_path / "TEST_Outline_Roe.pdf").write_text("x")
    r = _run(api, tmp_path)
    assert len([d for d in r.docs if d.name == "TEST_Outline_Roe.pdf"]) == 2  # two pages link it
    assert not any(d.name == "TEST_Outline_Roe.pdf" for d in r.missing_docs)


def test_revised_remote_files_are_flagged(tmp_path):
    api = _full_course()
    api.json_routes[STRUCT][1].append(
        _topic(15, "Slides", "/content/enforced/1234-X/W1.pdf", LastModifiedDate="2026-09-10T19:00:00.000Z"))
    api.json_routes[f"{LE}/dropbox/folders/"] = (200, [
        {"Id": 7, "Name": "Lab 1", "Attachments": [{"FileId": 70, "FileName": "lab1.pdf", "Size": 999}]}])
    import os
    (tmp_path / "W1.pdf").write_text("old")
    old = datetime(2026, 9, 9, tzinfo=UTC).timestamp()
    os.utime(tmp_path / "W1.pdf", (old, old))
    (tmp_path / "lab1.pdf").write_text("short")
    r = _run(api, tmp_path)
    revised = {d.name: d.note for d in r.revised}
    assert revised["W1.pdf"].startswith("remote changed")
    assert revised["lab1.pdf"] == "remote 999 bytes, local 5"
    assert "Remote files changed after the local copy" in render(r)


def test_announcement_attachments_carry_download_ids(tmp_path):
    api = _full_course()
    api.json_routes[f"{LE}/news/"] = (200, [{"Id": 42, "Title": "Welcome", "Body": {"Html": ""},
                                             "Attachments": [{"FileId": 9, "FileName": "info.pdf", "Size": 3}]}])
    r = _run(api, tmp_path)
    assert "`download_announcement_file news_id=42 file_id=9`" in render(r)


def test_outline_candidates_render_how_to_fetch_them():
    api = _full_course()
    api.json_routes[f"{LE}/news/"] = (200, [{"Title": "Welcome", "Attachments": [], "Body": {"Html": (
        f'<a href="{BASE}/d2l/common/dialogs/quickLink/quickLink.d2l?ou=1&type=content&rcode=Z">'
        'course outline here.</a>')}}])
    lines = [ln for ln in render(_run(api)).splitlines() if "Welcome" in ln and "outline" in ln]
    # A content quickLink is a page: the hint says browser, not download_linked_file.
    assert any("open in a browser:" in ln and "rcode=Z" in ln for ln in lines)
    assert not any("download_linked_file" in ln for ln in lines)
    lines = render(_run(_full_course())).splitlines()
    assert any("overview attachment" in ln and "download_course_overview" in ln for ln in lines)


def test_classlist_is_an_allowlist():
    api = _full_course()
    api.json_routes[f"{LE}/classlist/"] = (200, [
        {"DisplayName": "Teacher, Terry", "ClasslistRoleDisplayName": "Instructor"},
        {"DisplayName": "Helper, Hana", "ClasslistRoleDisplayName": "Teaching Assistant"},
        {"DisplayName": "Classmate, Casey", "ClasslistRoleDisplayName": "Auditor"},
        {"DisplayName": "Guest, Gil", "ClasslistRoleDisplayName": "Guest"},
    ])
    r = _run(api)
    assert r.staff == ["Teacher, Terry (Instructor)", "Helper, Hana (Teaching Assistant)"]
    report = render(r)
    assert "Casey" not in report and "Gil" not in report
    assert "roles not reported: Auditor, Guest" in report


@pytest.mark.parametrize("text", ["No GenAI tools.", "Gemini is banned.", "Do not ask Claude.",
                                  "large language models are out", "gen-AI use"])
def test_ai_mentions_cover_named_tools(text):
    assert ai_mentions(text)


def test_syncignore_keeps_user_deleted_files_out_of_missing(tmp_path):
    # A file the user deleted on purpose must not come back as "missing" on every run.
    (tmp_path / ".syncignore").write_text("# deleted by the user\nRUBRIC.pdf\nCourse Outline5.pdf\n\n")
    r = _run(_full_course(), tmp_path)
    missing = {d.name for d in r.missing_docs}
    ignored = {d.name for d in r.ignored_docs}
    assert "rubric.pdf" not in missing and "rubric.pdf" in ignored
    # An entry for X.pdf also covers a remote X.html, since HTML is kept as PDF.
    assert "Course Outline5.html" not in missing and "Course Outline5.html" in ignored
    assert "formulas.pdf" in missing
    text = render(r)
    assert "Ignored by .syncignore" in text and "rubric.pdf" in text.split("Ignored by .syncignore")[1]


def test_no_syncignore_changes_nothing(tmp_path):
    r = _run(_full_course(), tmp_path)
    assert r.ignored_docs == []
    assert "rubric.pdf" in {d.name for d in r.missing_docs}


# --- findings from the 2026-09-24 silent-failure review -------------------------


def test_unreadable_syncignore_is_unknown_not_empty(tmp_path):
    # Only a missing file means "nothing ignored". One that exists and cannot be
    # read used to count as empty, and every deleted file came back as missing.
    (tmp_path / ".syncignore").mkdir()
    r = _run(_full_course(), tmp_path)
    status = {s.name: s.status for s in r.sources}
    assert status[".syncignore"].startswith("UNKNOWN (IsADirectoryError")
    assert "**1 UNKNOWN**" in render(r)


def test_unknown_home_page_renders_no_navbar_negative():
    api = _full_course()
    api.text_routes[f"/d2l/home/{OU}"] = (500, "")
    report = render(_run(api))
    assert "navbar tools not read.**" not in report
    assert "Navbar tools not read: UNKNOWN (source course home page unreadable)" in report
    assert "Every navbar tool was read" not in report
    section = report.split("## Navbar tools this audit does not read")[1].split("##")[0]
    assert "UNKNOWN (source course home page unreadable)" in section


def test_unknown_due_sources_render_no_due_negative():
    api = _full_course()
    api.json_routes[f"{LE}/dropbox/folders/"] = (500, None)
    api.json_routes[f"{LE}/quizzes/"] = (403, None)
    section = render(_run(api)).split("## Due items")[1].split("##")[0]
    assert "None." not in section
    assert "UNKNOWN (sources dropbox folders, quizzes unreadable)" in section
    # One source unreadable, items from the other: listed, and flagged incomplete.
    api = _full_course()
    api.json_routes[f"{LE}/quizzes/"] = (403, None)
    section = render(_run(api)).split("## Due items")[1].split("##")[0]
    assert "Prelab 1" in section and "Incomplete: UNKNOWN (source quizzes unreadable)" in section


def test_unknown_html_sources_render_no_ai_negative():
    api = FakeAPI(json_routes={f"{LE}/overview": (500, None)}, text_routes={f"/d2l/home/{OU}": (200, NAV)})
    section = render(_run(api)).split("## AI mentions in HTML sources")[1].split("##")[0]
    assert "None in any HTML source read" not in section
    assert "UNKNOWN (sources overview, content tree, announcements, dropbox folders, quizzes unreadable)" in section


def test_dropbox_link_attachments_are_audited(tmp_path):
    api = _full_course()
    api.json_routes[f"{LE}/dropbox/folders/"] = (200, [{"Id": 7, "Name": "Lab 1", "LinkAttachments": [
        {"LinkId": 1, "LinkName": "Lab 1 handout", "Href": "/content/enforced/1234-X/lab1_handout.pdf"},
        {"LinkId": 2, "LinkName": "Simulator", "Href": "https://phet.example.org/sim"},
    ]}])
    r = _run(api, tmp_path)
    doc = next(d for d in r.missing_docs if d.name == "lab1_handout.pdf")
    assert doc.where == "dropbox 'Lab 1'" and doc.context == "Lab 1"
    assert doc.href == "/content/enforced/1234-X/lab1_handout.pdf"
    assert ("dropbox 'Lab 1'", "Simulator", "https://phet.example.org/sim") in r.external_links


def test_closed_topics_are_listed_apart_from_missing(tmp_path):
    # Past its EndDate a topic's file 403s. Counting it as missing asked for a
    # download that can never succeed.
    api = _full_course()
    api.json_routes[STRUCT][1].extend([
        _topic(30, "Week 0 slides", "/content/enforced/1234-X/W0.pdf", EndDate="2026-09-09T06:59:00.000Z"),
        _topic(31, "Old manual", "/content/enforced/1234-X/OldManual.pdf", kind=3,
               EndDate="2026-09-09T06:59:00.000Z"),
        _topic(32, "Week 1 slides", "/content/enforced/1234-X/W1.pdf", EndDate="2026-12-01T00:00:00.000Z"),
    ])
    r = _run(api, tmp_path)
    missing = {d.name for d in r.missing_docs}
    closed = {d.name: d for d in r.closed_docs}
    assert set(closed) == {"W0.pdf", "OldManual.pdf"}
    assert "W0.pdf" not in missing and "OldManual.pdf" not in missing and "W1.pdf" in missing
    assert closed["W0.pdf"].note == "closed Tue 2026-09-08 23:59 PDT"
    report = render(r)
    section = report.split("## Closed topics not present locally")[1].split("##")[0]
    assert "W0.pdf, closed Tue 2026-09-08 23:59 PDT" in section
    assert "W0.pdf" not in report.split("## Documents not present locally")[1].split("##")[0]


def test_unrecognised_list_shape_is_unknown_not_empty():
    api = _full_course()
    api.json_routes[f"{LE}/news/"] = (200, {"Something": []})
    api.json_routes[f"{LE}/checklists/"] = (200, {"Objects": None})
    status = {s.name: s.status for s in _run(api).sources}
    assert status["announcements"] == "UNKNOWN (HTTP -1)"
    assert status["checklists"] == "UNKNOWN (HTTP -1)"


def test_get_list_follows_bookmark_and_next():
    api = _full_course()
    api.json_routes[f"{LE}/classlist/"] = (200, {
        "PagingInfo": {"Bookmark": "b1", "HasMoreItems": True},
        "Items": [{"DisplayName": "Teacher, Terry", "ClasslistRoleDisplayName": "Instructor"}]})
    api.json_routes[f"{LE}/classlist/?bookmark=b1"] = (200, {
        "PagingInfo": {"Bookmark": "b2", "HasMoreItems": False},
        "Items": [{"DisplayName": "Helper, Hana", "ClasslistRoleDisplayName": "Teaching Assistant"}]})
    api.json_routes[f"{LE}/news/"] = (200, {"Objects": [{"Title": "One"}], "Next": f"{BASE}/next-news"})
    api.json_routes["/next-news"] = (200, {"Objects": [{"Title": "Two"}], "Next": None})
    r = _run(api)
    assert r.staff == ["Teacher, Terry (Instructor)", "Helper, Hana (Teaching Assistant)"]
    assert {s.name: s.detail for s in r.sources}["announcements"] == "2 items"


def test_paging_that_loops_or_has_no_bookmark_is_unknown():
    api = _full_course()
    api.json_routes[f"{LE}/news/"] = (200, {"Objects": [], "Next": f"{BASE}{LE}/news/"})
    api.json_routes[f"{LE}/checklists/"] = (200, {"PagingInfo": {"HasMoreItems": True}, "Items": []})
    status = {s.name: s.status for s in _run(api).sources}
    assert status["announcements"] == "UNKNOWN (HTTP -1)"
    assert status["checklists"] == "UNKNOWN (HTTP -1)"
