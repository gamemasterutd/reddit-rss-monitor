import email.utils
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .db import get_governor, update_governor
from .models import FetchResult


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class CooldownActive(RuntimeError):
    def __init__(self, until):
        super().__init__("Cooldown active until %s" % until)
        self.until = until


class RedditHTTP:
    def __init__(self, conn, user_agent, minimum_delay_seconds=120, timeout=30,
                 cooldown_429_hours=6, cooldown_429_repeat_hours=24, cooldown_403_hours=24,
                 sleeper=time.sleep, clock=None):
        self.conn = conn
        self.user_agent = user_agent
        self.minimum_delay_seconds = float(minimum_delay_seconds)
        self.timeout = timeout
        self.cooldown_429_hours = float(cooldown_429_hours)
        self.cooldown_429_repeat_hours = float(cooldown_429_repeat_hours)
        self.cooldown_403_hours = float(cooldown_403_hours)
        self.sleeper = sleeper
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def cooldown_until(self):
        gov = get_governor(self.conn)
        until = _parse_iso(gov["cooldown_until"])
        now = self.clock()
        if until and until > now:
            return _iso(until)
        if until and until <= now:
            update_governor(self.conn, cooldown_until=None)
        return None

    def _wait_for_governor(self, progress=None):
        until = self.cooldown_until()
        if until:
            raise CooldownActive(until)
        gov = get_governor(self.conn)
        last = _parse_iso(gov["last_request_at"])
        if not last:
            return
        elapsed = (self.clock() - last).total_seconds()
        wait = self.minimum_delay_seconds - elapsed
        if wait > 0:
            if progress:
                progress("Rate guard: waiting %d seconds..." % int(round(wait)))
            self.sleeper(wait)

    def _headers(self, conditional):
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if conditional:
            if conditional.get("etag"):
                headers["If-None-Match"] = conditional["etag"]
            if conditional.get("last_modified"):
                headers["If-Modified-Since"] = conditional["last_modified"]
        return headers

    def _open(self, request):
        return urllib.request.urlopen(request, timeout=self.timeout)

    def _retry_after_seconds(self, value):
        if not value:
            return 0
        value = value.strip()
        try:
            return max(0, int(value))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, int((dt - self.clock()).total_seconds()))
            except Exception:
                return 0

    def _set_cooldown(self, status, headers):
        now = self.clock()
        gov = get_governor(self.conn)
        consecutive = int(gov["consecutive_429"] or 0)
        if status == 429:
            consecutive += 1
            hours = self.cooldown_429_repeat_hours if consecutive >= 2 else self.cooldown_429_hours
            retry_after = self._retry_after_seconds(headers.get("Retry-After") or headers.get("retry-after"))
            delta = max(hours * 3600, retry_after)
            until = now + timedelta(seconds=delta)
            update_governor(
                self.conn,
                cooldown_until=_iso(until),
                consecutive_429=consecutive,
                last_http_status=status,
            )
            return _iso(until)
        if status == 403:
            until = now + timedelta(hours=self.cooldown_403_hours)
            update_governor(
                self.conn,
                cooldown_until=_iso(until),
                last_http_status=status,
            )
            return _iso(until)
        return None

    def fetch(self, url, conditional=None, progress=None):
        self._wait_for_governor(progress)
        request = urllib.request.Request(url, headers=self._headers(conditional or {}), method="GET")
        now = self.clock()
        update_governor(self.conn, last_request_at=_iso(now))
        try:
            with self._open(request) as response:
                body = response.read()
                headers = {k: v for k, v in response.headers.items()}
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                body = exc.read()
            except Exception:
                body = b""
            headers = {k: v for k, v in exc.headers.items()} if exc.headers else {}
        except urllib.error.URLError as exc:
            update_governor(self.conn, last_http_status=0)
            return FetchResult(0, b"", {}, url, error=str(exc.reason))
        except OSError as exc:
            update_governor(self.conn, last_http_status=0)
            return FetchResult(0, b"", {}, url, error=str(exc))

        update_governor(self.conn, last_http_status=status)
        if status in (429, 403):
            until = self._set_cooldown(status, headers)
            return FetchResult(status, body, headers, url, stopped=True, cooldown_until=until)
        if 200 <= status < 400:
            update_governor(self.conn, consecutive_429=0)
        return FetchResult(status, body, headers, url)
