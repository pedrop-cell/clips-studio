"""Viral GTA 6 videos other channels already posted, sent to Telegram as ideas.

Every `every_min` minutes it searches YouTube (Shorts included) for each query
in `trends.queries`, sorted by views and limited to this week's uploads, then
ranks what it found by views per hour since upload. The top new ones go to
the same Telegram chat as the clips: title, channel, views, age, link and an
idea for a video of your own built on it.

It suggests, it never downloads or reposts. Reposting someone else's video
earns copyright claims and strikes; reacting, commenting, testing a
curiosity yourself or a compilation with credit is what the platforms (and
the creators) accept.

No API key: the search goes through yt-dlp, which the clipper already needs.
TikTok has no free search that works without an account, so the message ends
with links to TikTok's GTA 6 pages to open by hand.

python -m live_clipper.trends --once     search now and send, then exit
"""

import argparse
import json
import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("live_clipper.trends")

# YouTube's search filter for "this week, sorted by view count" and
# "today, sorted by view count" (the sp= parameter of a results URL).
PERIOD_FILTER = {"semana": "CAMSAggD", "hoje": "CAMSAggC"}

TIKTOK_LINKS = [
    "https://www.tiktok.com/tag/gta6",
    "https://www.tiktok.com/search?q=gta%206%20curiosidades",
]

WARNING = ("⚠️ Não reposte o vídeo dos outros inteiro: dá strike e derruba a conta. "
           "Use como ideia, reaja ou comente por cima, e dê crédito.")

# First match wins: (words in the title, what to do with it).
IDEAS = [
    (r"curios|segredo|secret|easter|detalhe|detail|escondid|hidden|sabia|you missed",
     "Teste você mesmo no jogo e grave: \"Testei a curiosidade que tá viralizando\"."),
    (r"bug|glitch|falha|exploit|dinheiro infinito|money",
     "Tente reproduzir o bug ao vivo ou em gravação e mostre se funciona mesmo."),
    (r"\bvs\b|versus|compar|gta 5|gta v\b|gta5|antes e depois",
     "Faça um comparativo lado a lado com a sua gameplay e sua opinião em cima."),
    (r"trailer|leak|vaz|rumor|data|price|preço|pre[cç]o",
     "Reaja em tela dividida comentando o que importa pra quem é do Brasil."),
    (r"rea[cç]|react|streamer|live|ao vivo",
     "Junte as melhores reações de vários streamers numa compilação com créditos."),
    (r"dica|tip|como|how to|guia|guide|tutorial",
     "Refaça a dica em 30s, direto ao ponto, com a sua gameplay e legenda em português."),
    (r"engra|funny|kkk|lol|fail|meme",
     "Faça um top 5 dos momentos mais engraçados, cada um com crédito na tela."),
]
DEFAULT_IDEA = "Use o assunto num vídeo seu: comente por cima em tela dividida e dê crédito."


@dataclass
class TrendsConfig:
    enabled: bool = True
    every_min: int = 60               # how often to search
    top: int = 6                      # videos per Telegram message
    min_views: int = 20000            # ignore smaller ones
    period: str = "semana"            # semana | hoje
    per_query: int = 15               # search results read per query
    queries: list[str] = field(default_factory=lambda: [
        "GTA 6", "GTA 6 curiosidades", "GTA 6 easter egg", "GTA 6 viral",
        "GTA 6 shorts", "GTA VI gameplay",
    ])

    @classmethod
    def from_raw(cls, raw: dict | None) -> "TrendsConfig":
        raw = dict(raw or {})
        base = cls()
        period = str(raw.get("period", base.period)).lower()
        return cls(
            enabled=bool(raw.get("enabled", base.enabled)),
            every_min=max(15, int(raw.get("every_min", base.every_min))),
            top=max(1, int(raw.get("top", base.top))),
            min_views=int(raw.get("min_views", base.min_views)),
            period=period if period in PERIOD_FILTER else base.period,
            per_query=max(1, int(raw.get("per_query", base.per_query))),
            queries=[str(q) for q in (raw.get("queries") or base.queries)],
        )


@dataclass
class Video:
    id: str
    title: str
    channel: str
    url: str
    views: int
    duration: float | None = None
    timestamp: float | None = None    # upload time, when known
    likes: int | None = None

    @property
    def is_short(self) -> bool:
        return "/shorts/" in self.url or (self.duration is not None and self.duration <= 65)

    def age_h(self, now: float) -> float | None:
        if self.timestamp is None:
            return None
        return max(1.0, (now - self.timestamp) / 3600)

    def score(self, now: float) -> float:
        """Views per hour since upload. Unknown upload time counts as two days old."""
        age = self.age_h(now) or 48.0
        s = self.views / age
        if self.likes and self.views:
            s *= 1 + min(self.likes / self.views, 0.2)  # a well-liked video spreads further
        return s


# ---- search -------------------------------------------------------------------


def search_url(query: str, period: str) -> str:
    q = urllib.parse.quote_plus(query)
    return f"https://www.youtube.com/results?search_query={q}&sp={PERIOD_FILTER[period]}"


def _ydl(opts: dict):
    import yt_dlp

    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, **opts})


def ytdlp_search(query: str, period: str, limit: int) -> list[dict]:
    """Flat search results (id, title, channel, view_count, duration, url)."""
    with _ydl({"extract_flat": "in_playlist", "playlistend": limit}) as ydl:
        info = ydl.extract_info(search_url(query, period), download=False) or {}
    return [e for e in (info.get("entries") or []) if e][:limit]


def ytdlp_details(url: str) -> dict:
    """Upload time and likes, which flat search results leave out."""
    with _ydl({"ignore_no_formats_error": True}) as ydl:
        return ydl.extract_info(url, download=False, process=False) or {}


def to_video(entry: dict) -> Video | None:
    vid = entry.get("id")
    if not vid or entry.get("live_status") in ("is_live", "is_upcoming"):
        return None
    url = entry.get("url") or entry.get("webpage_url") or ""
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    return Video(
        id=str(vid), title=str(entry.get("title") or "").strip(),
        channel=str(entry.get("channel") or entry.get("uploader") or "").strip(),
        url=url, views=int(entry.get("view_count") or 0),
        duration=entry.get("duration"),
    )


def collect(cfg: TrendsConfig, seen: set[str], now: float,
            search=ytdlp_search, details=ytdlp_details) -> list[Video]:
    """The top new videos across every query, best first."""
    found: dict[str, Video] = {}
    for q in cfg.queries:
        try:
            entries = search(q, cfg.period, cfg.per_query)
        except Exception as e:  # one bad query must not stop the rest
            log.warning("trends: search %r failed: %s", q, e)
            continue
        for e in entries:
            v = to_video(e)
            if v and v.id not in seen and v.views >= cfg.min_views and v.id not in found:
                found[v.id] = v
    # Ask for upload time and likes only for the ones that can make the cut.
    shortlist = sorted(found.values(), key=lambda v: v.views, reverse=True)[: cfg.top * 3]
    for v in shortlist:
        try:
            d = details(v.url)
        except Exception as e:
            log.debug("trends: details for %s failed: %s", v.id, e)
            continue
        ts = d.get("timestamp") or d.get("release_timestamp")
        if ts is None and d.get("upload_date"):
            ts = time.mktime(time.strptime(str(d["upload_date"]), "%Y%m%d"))
        v.timestamp = float(ts) if ts else None
        v.likes = d.get("like_count")
        v.views = int(d.get("view_count") or v.views)
        v.duration = d.get("duration") or v.duration
    max_age_h = 26 if cfg.period == "hoje" else 8 * 24
    fresh = [v for v in shortlist if (v.age_h(now) or 0) <= max_age_h]
    return sorted(fresh, key=lambda v: v.score(now), reverse=True)[: cfg.top]


# ---- message ------------------------------------------------------------------


def human(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} mi".replace(".", ",")
    if n >= 1_000:
        return f"{n / 1_000:.0f} mil"
    return str(int(n))


def idea_for(title: str) -> str:
    t = title.lower()
    for pattern, idea in IDEAS:
        if re.search(pattern, t):
            return idea
    return DEFAULT_IDEA


def age_text(hours: float | None) -> str:
    if hours is None:
        return "esta semana"
    if hours < 24:
        return f"há {int(hours)}h"
    return f"há {int(hours // 24)} dia(s)"


def digest(videos: list[Video], now: float) -> str:
    lines = ["🔥 GTA 6 viralizando agora (outros canais)", ""]
    for i, v in enumerate(videos, 1):
        kind = "Short" if v.is_short else "Vídeo"
        lines += [
            f"{i}. {v.title[:110]}",
            f"   {kind} de {v.channel or '?'} · {human(v.views)} views · {age_text(v.age_h(now))}"
            f" · {human(v.score(now))}/h",
            f"   {v.url}",
            f"   💡 {idea_for(v.title)}",
            "",
        ]
    lines += ["TikTok (abra e veja o que está bombando):", *TIKTOK_LINKS, "", WARNING]
    return "\n".join(lines)


# ---- seen list -----------------------------------------------------------------


class Seen:
    """Video ids already sent, so each one is suggested once. Forgets after 14 days."""

    KEEP_S = 14 * 24 * 3600

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.data: dict[str, float] = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def ids(self) -> set[str]:
        return set(self.data)

    def add(self, ids: list[str], now: float) -> None:
        self.data.update(dict.fromkeys(ids, now))
        self.data = {k: t for k, t in self.data.items() if now - t < self.KEEP_S}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data), "utf-8")


# ---- run ----------------------------------------------------------------------


def run_once(cfg: TrendsConfig, telegram, seen: Seen, now: float | None = None, **kw) -> int:
    """Search, send one message if anything new turned up. Returns how many were sent."""
    now = time.time() if now is None else now
    videos = collect(cfg, seen.ids(), now, **kw)
    if not videos:
        log.info("trends: nothing new above %d views", cfg.min_views)
        return 0
    text = digest(videos, now)
    if telegram is None:
        print(text)
    else:
        telegram.send_text(text, preview=False)
    seen.add([v.id for v in videos], now)
    log.info("trends: sent %d viral videos", len(videos))
    return len(videos)


class TrendsWorker(threading.Thread):
    """Runs next to the live clipper: one search every `every_min` minutes."""

    def __init__(self, cfg: TrendsConfig, telegram, work_dir: Path, stop: threading.Event):
        super().__init__(name="trends", daemon=True)
        self.cfg, self.telegram, self.stop = cfg, telegram, stop
        self.seen = Seen(Path(work_dir) / "trends_seen.json")

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                run_once(self.cfg, self.telegram, self.seen)
            except Exception:
                log.exception("trends: search failed")
            self.stop.wait(self.cfg.every_min * 60)


def main() -> None:
    from live_clipper import config, envfile
    from live_clipper.notify import Telegram

    ap = argparse.ArgumentParser(prog="live_clipper.trends",
                                 description="Send viral GTA 6 videos from other channels to Telegram.")
    ap.add_argument("--config", default="live_clipper.yaml")
    ap.add_argument("--env", default="clipador.env")
    ap.add_argument("--once", action="store_true", help="search once and exit (default: keep running)")
    ap.add_argument("--print", action="store_true", help="print the message instead of sending it")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    envfile.load(Path(args.env))
    envfile.load(Path(".env"))
    cfg = config.load(args.config)
    tg = None if args.print else Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    seen = Seen(cfg.work_dir / "trends_seen.json")
    if args.once:
        print("Procurando vídeos virais de GTA 6... (leva 1 a 2 minutos)")
        n = run_once(cfg.trends, tg, seen)
        print(f"Pronto: {n} vídeo(s) novo(s) mandado(s) pro Telegram." if n
              else "Nada novo desde a última busca.")
        return
    TrendsWorker(cfg.trends, tg, cfg.work_dir, threading.Event()).run()


if __name__ == "__main__":
    main()
