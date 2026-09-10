from pydantic import BaseModel
from typing import Literal, Optional


class TranscribeRequest(BaseModel):
    abs_item_id: str
    mode: Literal["full", "chapter", "range"]
    chapter_index: Optional[int] = None   # required if mode=chapter
    from_chapter: Optional[int] = None    # required if mode=range
    count: Optional[int] = None           # required if mode=range


class JobStatus(BaseModel):
    job_id: str
    book_id: str
    chapter_index: int
    status: str
    progress: float = 0.0
    vtt_url: Optional[str] = None
    error_message: Optional[str] = None


class ChapterMeta(BaseModel):
    index: int
    title: str
    start: float
    end: float
    duration: float


class BookMeta(BaseModel):
    item_id: str
    title: str
    author: str
    cover_url: str
    duration: float
    chapters: list[ChapterMeta]
