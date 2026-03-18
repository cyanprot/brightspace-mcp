from pydantic import BaseModel


class Course(BaseModel):
    id: int
    name: str
    code: str | None = None
    is_active: bool = True


class Assignment(BaseModel):
    id: int
    name: str
    course_name: str = ""
    due_date: str | None = None
    points: float | None = None
    instructions_snippet: str | None = None


class CalendarEvent(BaseModel):
    id: int
    title: str
    course_name: str = ""
    course_id: int = 0
    start_date: str | None = None
    end_date: str | None = None
    is_all_day: bool = False


class ContentItem(BaseModel):
    id: int
    title: str
    item_type: str  # "module" or "topic"
    topic_type: str | None = None  # "file", "link", "scorm", or None
    url: str | None = None
    parent_module_id: int | None = None
    due_date: str | None = None
    last_modified: str | None = None
    is_hidden: bool = False
    has_children: bool = False


class DownloadResult(BaseModel):
    topic_id: int
    filename: str
    save_path: str
    size_bytes: int


class GradeValue(BaseModel):
    grade_item_id: str
    name: str
    points_numerator: float | None = None
    points_denominator: float | None = None
    displayed_grade: str | None = None
    grade_type: int = 1  # 1=Numeric, 2=PassFail, 3=SelectBox, 4=Text, 7=Calculated
