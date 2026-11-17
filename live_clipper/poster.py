"""How much gets posted, and what the post says.

TikTok refuses posts past roughly 15 a day per account through the API and
flags bursts as spam, which no retry fixes. So every platform gets a daily
cap and a minimum gap between posts, counted on disk so a restart in the
middle of launch night does not reset them.
"""

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Limits:
    per_day: int = 12
    min_gap_s: int = 1200


@dataclass
class Ledger:
    """Posts made per platform, kept in a JSON file."""

    path: Path
    utc_offset_h: float = -3.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _load(self) -> dict[str, list[float]]:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), "utf-8")
        tmp.replace(self.path)

    def _day(self, ts: float) -> str:
        tz = timezone(timedelta(hours=self.utc_offset_h))
        return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")

    def allowed(self, platforms: list[str], limits: dict[str, Limits], now: float | None = None) -> list[str]:
        now = now or time.time()
        with self._lock:
            data = self._load()
        out = []
        for p in platforms:
            lim = limits.get(p, Limits())
            times = data.get(p, [])
            today = [t for t in times if self._day(t) == self._day(now)]
            if len(today) >= lim.per_day:
                continue
            if times and now - max(times) < lim.min_gap_s:
                continue
            out.append(p)
        return out

    def record(self, platforms: list[str], now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            data = self._load()
            for p in platforms:
                kept = [t for t in data.get(p, []) if now - t < 3 * 86400]
                data[p] = [*kept, now]
            self._save(data)


_TAG = re.compile(r"[^\w]", re.UNICODE)


def clean_tags(*groups: list[str]) -> list[str]:
    seen: list[str] = []
    for group in groups:
        for t in group or []:
            t = _TAG.sub("", str(t).lstrip("#"))
            if t and t.lower() not in (s.lower() for s in seen):
                seen.append(t)
    return seen


@dataclass(frozen=True)
class Post:
    title: str
    description: str
    tags: list[str]
    overrides: dict


def compose(clip: dict, *, channel: str, platform: str, source_url: str,
            hashtags: list[str], credit_template: str, platforms: list[str],
            privacy: dict[str, str]) -> Post:
    """Title, description and per-platform fields for one clip.

    Every post credits the streamer: programs that pay clippers require it,
    and it is the decent thing to do with someone else's stream.
    """
    tags = clean_tags(hashtags, clip.get("hashtags") or [])
    credit = credit_template.format(channel=channel, platform=platform, url=source_url)
    title = (clip.get("title") or f"{channel} ao vivo").strip()[:95]
    tag_line = " ".join(f"#{t}" for t in tags)
    description = "\n\n".join(x for x in ((clip.get("description") or "").strip(), credit, tag_line) if x)
    overrides: dict[str, dict] = {}
    if "tiktok" in platforms:
        # TikTok has no description field: the title is the whole caption.
        overrides["tiktok"] = {"title": f"{title} {credit} {tag_line}".strip()[:2150]}
    if "instagram" in platforms:
        overrides["instagram"] = {"title": f"{title}\n\n{credit}\n\n{tag_line}".strip()[:2150]}
    for p, level in privacy.items():
        if p in platforms and level:
            overrides.setdefault(p, {})["privacy"] = level
    return Post(title=title, description=description, tags=tags, overrides=overrides)
