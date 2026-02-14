from typing import List, Optional
from pydantic import BaseModel, HttpUrl
from datetime import datetime

# Article 模型
class Article(BaseModel):
    id: int
    url: HttpUrl
    html_url: HttpUrl
    author_id: int

    comments_disabled: bool
    draft: bool
    promoted: bool

    position: int
    vote_sum: int
    vote_count: int
    section_id: int

    created_at: datetime
    updated_at: datetime
    edited_at: datetime

    name: str
    title: str

    source_locale: str
    locale: str

    outdated: bool
    outdated_locales: List[str]

    user_segment_id: Optional[int] = None
    user_segment_ids: List[int]

    permission_group_id: int
    content_tag_ids: List[int]
    label_names: List[str]

    body: str

# 分页响应模型
class ArticleListResponse(BaseModel):
    count: int
    next_page: Optional[HttpUrl] = None
    previous_page: Optional[HttpUrl] = None

    page: int
    page_count: int
    per_page: int

    sort_by: str
    sort_order: str

    articles: List[Article]
