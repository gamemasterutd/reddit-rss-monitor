from dataclasses import dataclass, field
from typing import Dict, List, Optional

@dataclass
class FeedEntry:
    post_id: str
    subreddit: str
    title: str
    description: str
    permalink: str
    published: str

@dataclass
class FilterResult:
    classification: str
    topic: str
    score: float
    matched_terms: List[str] = field(default_factory=list)
    review_reason: str = ""

@dataclass
class FetchResult:
    status: int
    body: bytes
    headers: Dict[str, str]
    url: str
    error: str = ""
    stopped: bool = False
    cooldown_until: Optional[str] = None
