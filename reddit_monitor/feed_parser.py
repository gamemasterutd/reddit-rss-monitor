import html
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse
import xml.etree.ElementTree as ET

from .models import FeedEntry

ATOM = "{http://www.w3.org/2005/Atom}"

class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.parts.append(value)
    def get_text(self):
        return " ".join(self.parts).strip()

def html_to_text(value):
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html.unescape(value))
        parser.close()
        return parser.get_text()
    except Exception:
        return " ".join(html.unescape(value).split())


class _MediaExtractor(HTMLParser):
    """Keep public image/video links supplied inside an RSS item; never fetch them."""

    HOSTS = {"i.redd.it", "preview.redd.it", "external-preview.redd.it", "v.redd.it"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for key in ("src", "href"):
            raw = html.unescape(values.get(key, ""))
            if not raw:
                continue
            parsed = urlparse(raw)
            if parsed.scheme in ("http", "https") and parsed.hostname in self.HOSTS:
                if raw not in self.urls:
                    self.urls.append(raw)


def extract_media_urls(value):
    if not value:
        return []
    parser = _MediaExtractor()
    try:
        parser.feed(html.unescape(value))
        parser.close()
    except Exception:
        return []
    return parser.urls[:5]


def description_with_media(value):
    description = html_to_text(value)
    media = extract_media_urls(value)
    if media:
        description += " Attached media URLs (not inspected): " + " ".join(media)
    return description

def normalize_timestamp(value):
    if not value:
        return ""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # RFC 822 used by RSS 2.0.
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def canonical_permalink(url):
    if not url:
        return ""
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if host in {"reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com"}:
        host = "www.reddit.com"
    path = re.sub(r"/+", "/", parsed.path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urlunparse(("https", host, path, "", "", ""))

def extract_post_id(raw_id, permalink):
    raw_id = (raw_id or "").strip()
    m = re.search(r"\bt3_([A-Za-z0-9]+)\b", raw_id)
    if m:
        return m.group(1).lower()
    m = re.search(r"/comments/([A-Za-z0-9]+)/", (permalink or "") + "/")
    if m:
        return m.group(1).lower()
    return ""

def _atom_entries(root, subreddit):
    items = []
    for entry in root.findall(ATOM + "entry"):
        title = entry.findtext(ATOM + "title") or ""
        raw_id = entry.findtext(ATOM + "id") or ""
        published = entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated") or ""
        link = ""
        for link_el in entry.findall(ATOM + "link"):
            href = link_el.attrib.get("href", "")
            rel = link_el.attrib.get("rel", "alternate")
            if href and rel in {"alternate", ""}:
                link = href
                break
        content = entry.findtext(ATOM + "content") or entry.findtext(ATOM + "summary") or ""
        canonical = canonical_permalink(link)
        post_id = extract_post_id(raw_id, canonical)
        if not post_id or not canonical or not published:
            raise ValueError("Malformed Atom entry missing Reddit post id, permalink, or timestamp")
        items.append(FeedEntry(
            post_id=post_id,
            subreddit=subreddit,
            title=html_to_text(title),
            description=description_with_media(content),
            permalink=canonical,
            published=normalize_timestamp(published),
        ))
    return items

def _rss_entries(root, subreddit):
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        raw_id = item.findtext("guid") or ""
        published = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        description = item.findtext("description") or ""
        canonical = canonical_permalink(link)
        post_id = extract_post_id(raw_id, canonical)
        if not post_id or not canonical or not published:
            raise ValueError("Malformed RSS item missing Reddit post id, permalink, or timestamp")
        items.append(FeedEntry(
            post_id=post_id,
            subreddit=subreddit,
            title=html_to_text(title),
            description=description_with_media(description),
            permalink=canonical,
            published=normalize_timestamp(published),
        ))
    return items

def parse_feed(xml_bytes, subreddit):
    if not xml_bytes:
        return []
    root = ET.fromstring(xml_bytes)
    tag = root.tag.lower()
    if tag.endswith("feed"):
        return _atom_entries(root, subreddit)
    return _rss_entries(root, subreddit)
