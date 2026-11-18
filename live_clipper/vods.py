"""Today's finished lives (VODs) of the watched channels, clipped whole.

Once a day, at `vods.at` (local time), it lists each Twitch and YouTube
channel's past broadcasts, takes the ones that started in the last
`lookback_h` hours, and hands each full video to Clips Kitty, which finds the
best moments in it. The best `max_clips` clips of each go to Telegram like
the live clips. A video sent once is never sent again (dados/vods_done.json).

A full live is hours of video and the app works on one video at a time, so
the default time is early morning, when the lives are usually over and the
live clips are not waiting behind it. Kick is skipped: yt-dlp cannot list a
Kick channel's past broadcasts.

python -m live_clipper.vods          look now and send, then exit
"""

import argparse
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("live_clipper.vods")

SUPPORTED = ("twitch", "youtube")
NOT_READY = ("is_live", "is_upcoming", "post_live")


@dataclass
class VodConfig:
    enabled: bool = True
    at: str = "06:00"          # local time of the daily run
    lookback_h: int = 24       # lives that started this recently
    max_clips: int = 5         # clips sent to Telegram per live
    max_vods: int = 4          # lives sent to the app per run
    per_channel: int = 3       # newest broadcasts looked at per channel

    @classmethod
    def from_raw(cls, raw: dict | None) -> "VodConfig":
        raw = dict(raw or {})
        b = cls()
        at = str(raw.get("at", b.at)).strip()
        try:
            h, m = (int(x) for x in at.split(":"))
            at = f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else b.at
        except ValueError:
            at = b.at
        return cls(
            enabled=bool(raw.get("enabled", b.enabled)),
            at=at,
            lookback_h=max(1, int(raw.get("lookback_h", b.lookback_h))),
            max_clips=max(1, int(raw.get("max_clips", b.max_clips))),
            max_vods=max(1, int(raw.get("max_vods", b.max_vods))),
            per_channel=max(1, int(raw.get("per_channel", b.per_channel))),
        )


def listing_url(platform: str, name: str) -> str | None:
    """Where a channel's past broadcasts are listed (name as config stores it)."""
    if platform == "twitch":
        return f"https://www.twitch.tv/{name}/videos?filter=archives&sort=time"
    if platform == "youtube":
        if name.startswith("watch:"):
            return None
        if name.startswith("@"):
            return f"https://www.youtube.com/{name}/streams"
        return f"https://www.youtube.com/channel/{name}/streams"
    return None


def _extract(url: str, flat: bool, size: int) -> dict | None:
    import yt_dlp

    from live_clipper.recorder import _Silent

    opts: dict = {"quiet": True, "no_warnings": True, "skip_download": True,
                  "socket_timeout": 30, "logger": _Silent()}
    if flat:
        opts.update({"extract_flat": "in_playlist", "playlistend": size})
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False, process=False)


def recent_vods(platform: str, name: str, since: float, per_channel: int = 3,
                extract: Callable[[str, bool, int], dict | None] = _extract) -> list[dict]:
    """[{url, title, started, duration}] of finished broadcasts started after `since`."""
    url = listing_url(platform, name)
    if not url:
        return []
    try:
        listing = extract(url, True, per_channel) or {}
    except Exception as e:
        log.info("vods: %s/%s: listing failed: %s", platform, name, str(e)[:160])
        return []
    out = []
    for entry in [e for e in (listing.get("entries") or []) if e][:per_channel]:
        target = entry.get("url") or entry.get("webpage_url")
        if not target:
            continue
        try:
            info = extract(target, False, 1) or {}
        except Exception:
            continue
        if info.get("live_status") in NOT_READY or info.get("is_live"):
            continue
        started = info.get("release_timestamp") or info.get("timestamp")
        link = info.get("webpage_url") or target
        if not started or float(started) < since:
            continue
        out.append({"url": link, "title": info.get("title") or "", "started": float(started),
                    "duration": float(info.get("duration") or 0)})
    return out


class Done:
    """VOD links already handed to the app, kept on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.urls = set(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            self.urls = set()

    def __contains__(self, url: str) -> bool:
        return url in self.urls

    def add(self, url: str) -> None:
        self.urls.add(url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(self.urls)), "utf-8")


def run_once(cfg, clipper, now: float | None = None,
             find: Callable[..., list[dict]] = recent_vods) -> int:
    """Find today's lives, process each in the app, send the best clips. Returns lives sent."""
    vc = cfg.vods
    now = now or time.time()
    done = Done(cfg.work_dir / "vods_done.json")
    found = []
    for ch in cfg.channels:
        if ch.platform not in SUPPORTED:
            continue
        for v in find(ch.platform, ch.name, now - vc.lookback_h * 3600, vc.per_channel):
            if v["url"] not in done:
                found.append((ch, v))
    found.sort(key=lambda cv: cv[1]["started"])
    if not found:
        log.info("vods: no new finished lives in the last %dh", vc.lookback_h)
        return 0
    log.info("vods: %d new finished live(s); sending up to %d to Clips Kitty", len(found), vc.max_vods)
    clipper.engine.resume_queue()
    sent = 0
    for ch, v in found[:vc.max_vods]:
        if clipper.stop_event is not None and clipper.stop_event.is_set():
            break
        try:
            if process_vod(cfg, clipper, ch, v):
                sent += 1
        except Exception:
            log.exception("vods: %s: %s failed", ch.key, v["url"])
        done.add(v["url"])
    return sent


def process_vod(cfg, clipper, ch, v: dict) -> bool:
    from live_clipper.engine import best_clips

    options = {**cfg.engine_options, "max_clips": max(cfg.vods.max_clips, 3)}
    hours = v["duration"] / 3600
    log.info("vods: %s: sending %s (%.1fh) to Clips Kitty", ch.key, v["url"], hours)
    job_id = clipper.engine.submit_url(
        v["url"], options=options,
        on_full=lambda: log.info("vods: Clips Kitty queue is full; waiting for room"))
    if job_id is None:
        log.info("vods: %s: the app already has %s", ch.key, v["url"])
        return False

    def waiting(job: dict, waited: float) -> None:
        log.info("vods: %s: Clips Kitty job %s still %s after %.0f min", ch.key, job_id,
                 job.get("status"), waited / 60)

    job = clipper.engine.wait_job(job_id, timeout_s=12 * 3600, every_s=30, on_wait=waiting,
                                  report_every_s=900)
    if job.get("status") != "done":
        log.warning("vods: %s: job %s %s: %s", ch.key, job_id, job.get("status"), job.get("error"))
        return False
    video_id = job.get("video_id") or clipper.engine.job(job_id).get("video_id")
    clips = best_clips(clipper.engine.clips(video_id), cfg.vods.max_clips) if video_id else []
    if not clips:
        log.info("vods: %s: no clips from %s", ch.key, v["url"])
        return True
    name = ch.label or ch.name
    day = time.strftime("%d/%m", time.localtime(v["started"]))
    for c in clips:
        if clipper.telegram is not None:
            clipper._deliver(ch, c, f"live de {day}", name, v["url"])
    log.info("vods: %s: %d clip(s) from %s sent", ch.key, len(clips), v["url"])
    return True


def seconds_until(at: str, utc_offset_h: float, now: float | None = None) -> float:
    """Seconds from now to the next `at` (HH:MM) in the config's time zone."""
    now = now or time.time()
    h, m = (int(x) for x in at.split(":"))
    local = now + utc_offset_h * 3600
    day_start = local - (local % 86400)
    target = day_start + h * 3600 + m * 60
    if target <= local:
        target += 86400
    return target - local


class VodWorker(threading.Thread):
    def __init__(self, cfg, clipper, stop: threading.Event):
        super().__init__(name="vods", daemon=True)
        self.cfg, self.clipper, self.stop = cfg, clipper, stop
        clipper.stop_event = stop

    def run(self) -> None:
        while not self.stop.is_set():
            wait = seconds_until(self.cfg.vods.at, self.cfg.utc_offset_h)
            if self.stop.wait(wait):
                return
            try:
                run_once(self.cfg, self.clipper)
            except Exception:
                log.exception("vods: daily run failed")


def main() -> None:
    from live_clipper import config, envfile
    from live_clipper.engine import Engine
    from live_clipper.runner import Clipper

    ap = argparse.ArgumentParser(prog="live_clipper.vods",
                                 description="Clip today's finished lives of the watched channels.")
    ap.add_argument("--config", default="live_clipper.yaml")
    ap.add_argument("--env", default="clipador.env")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    envfile.load(Path(args.env))
    envfile.load(Path(".env"))
    cfg = config.load(args.config)
    engine = Engine(cfg.engine_url)
    if not engine.healthy():
        print("Abra o app Clips Kitty e rode de novo.")
        return
    print("Procurando as lives de hoje dos streamers da lista... Cada live leva um tempo no app;"
          " deixe esta janela aberta.")
    n = run_once(cfg, Clipper(cfg, engine))
    print(f"Pronto: {n} live(s) analisada(s); os cortes foram pro Telegram." if n
          else "Nenhuma live nova para analisar.")


if __name__ == "__main__":
    main()
