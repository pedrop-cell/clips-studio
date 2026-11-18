"""One worker per channel: wait for live, record, read chat, clip spikes.

Spikes are handed to a small shared pool, so a busy launch night with ten
channels spiking at once queues cuts instead of starting ten engine jobs.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from live_clipper.chat import reader_for
from live_clipper.config import Channel, Config
from live_clipper.detector import Spike, SpikeDetector
from live_clipper.engine import Engine, EngineError, best_clip
from live_clipper.notify import NotifyError, Telegram
from live_clipper.poster import Ledger, compose
from live_clipper.recorder import Recorder, is_live, live_url
from live_clipper.trends import TrendsWorker

log = logging.getLogger("live_clipper")


class Clipper:
    """Turns one spike into a published clip. Shared by every channel."""

    def __init__(self, cfg: Config, engine: Engine | None = None, ledger: Ledger | None = None):
        self.cfg = cfg
        self.engine = engine or Engine(cfg.engine_url)
        self.ledger = ledger or Ledger(cfg.work_dir / "posted.json", cfg.utc_offset_h)
        self.telegram = None
        if cfg.publish and cfg.mode == "telegram":
            self.telegram = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.pool = ThreadPoolExecutor(max_workers=max(1, cfg.max_parallel_jobs),
                                       thread_name_prefix="clip")

    def submit(self, ch: Channel, rec: Recorder, spike: Spike) -> None:
        self.pool.submit(self._safe, ch, rec, spike)

    def _safe(self, ch: Channel, rec: Recorder, spike: Spike) -> None:
        try:
            self.handle(ch, rec, spike)
        except Exception:
            log.exception("%s: clip at %s failed", ch.key, spike.at)

    def handle(self, ch: Channel, rec: Recorder, spike: Spike, sleep=time.sleep) -> str | None:
        t0 = spike.onset - self.cfg.pre_s
        t1 = spike.onset + self.cfg.post_s
        # Wait for the recording to pass the end of the window.
        deadline = time.time() + self.cfg.post_s + 60
        while rec.covered_until() < t1 and time.time() < deadline:
            sleep(2)
        stamp = datetime.fromtimestamp(spike.onset, timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = self.cfg.work_dir / "cuts" / ch.key / f"{stamp}.mp4"
        if rec.cut(t0, t1, out) is None:
            log.warning("%s: nothing recorded for %s", ch.key, stamp)
            return None
        log.info("%s: cut %s (chat x%.1f)", ch.key, out.name, spike.score)
        name = ch.label or ch.name
        url = live_url(ch.platform, ch.name)
        job_id, video_id = self.engine.submit_local(
            str(out), title=f"{name} ao vivo {stamp}", channel=ch.name, platform=ch.platform,
            source_url=url, options=self.cfg.engine_options,
        )
        job = self.engine.wait_job(job_id)
        if job.get("status") != "done":
            log.warning("%s: engine job %s %s: %s", ch.key, job_id, job.get("status"), job.get("error"))
            return None
        clip = best_clip(self.engine.clips(video_id))
        if clip is None:
            log.info("%s: engine made no clip from %s", ch.key, stamp)
            return None
        if not self.cfg.publish:
            log.info("%s: clip %s ready (publishing off): %s", ch.key, clip["id"], clip.get("path"))
            return clip.get("path")
        if self.telegram is not None:
            return self._deliver(ch, clip, spike, name, url)
        targets = self.ledger.allowed(self.cfg.platforms, self.cfg.limits)
        if not targets:
            log.info("%s: clip %s kept, every platform is at its cap or gap", ch.key, clip["id"])
            return clip.get("path")
        post = compose(clip, channel=name, platform=ch.platform, source_url=url,
                       hashtags=self.cfg.hashtags, credit_template=self.cfg.credit,
                       platforms=targets, privacy=self.cfg.privacy)
        try:
            self.engine.publish(int(clip["id"]), platforms=targets, title=post.title,
                                description=post.description, tags=post.tags, overrides=post.overrides)
        except EngineError as e:
            log.error("%s: publish of clip %s failed: %s", ch.key, clip["id"], e)
            return clip.get("path")
        self.ledger.record(targets)
        log.info("%s: clip %s sent to %s", ch.key, clip["id"], ", ".join(targets))
        return clip.get("path")


    def _deliver(self, ch: Channel, clip: dict, spike: Spike, name: str, url: str) -> str | None:
        post = compose(clip, channel=name, platform=ch.platform, source_url=url,
                       hashtags=self.cfg.hashtags, credit_template=self.cfg.credit,
                       platforms=["tiktok"], privacy={})
        short = f"{post.title}\n{name} · chat x{spike.score:.0f}"
        try:
            self.telegram.send_clip(Path(clip["path"]), short, post.overrides["tiktok"]["title"])
        except NotifyError as e:
            log.error("%s: telegram delivery of clip %s failed: %s", ch.key, clip["id"], e)
            return clip.get("path")
        log.info("%s: clip %s sent to Telegram", ch.key, clip["id"])
        return clip.get("path")


class ChannelWorker(threading.Thread):
    def __init__(self, ch: Channel, cfg: Config, clipper: Clipper, stop: threading.Event):
        super().__init__(name=f"chan-{ch.key}", daemon=True)
        self.ch, self.cfg, self.clipper, self.stop = ch, cfg, clipper, stop

    def run(self) -> None:
        while not self.stop.is_set():
            if is_live(self.ch.platform, self.ch.name):
                log.info("%s is live", self.ch.key)
                try:
                    self._live_session()
                except Exception:
                    log.exception("%s: live session crashed", self.ch.key)
            self.stop.wait(self.cfg.live_check_s)

    def _live_session(self) -> None:
        rec = Recorder(self.ch.platform, self.ch.name, self.cfg.work_dir / "buffer" / self.ch.key,
                       buffer_s=self.cfg.buffer_s)
        det = SpikeDetector(**vars(self.cfg.detection))
        lock = threading.Lock()

        def on_message(ts: float, text: str) -> None:
            with lock:
                det.add(ts, text)

        session_stop = threading.Event()
        chat = reader_for(self.ch.platform, self.ch.name)
        chat_thread = threading.Thread(target=chat.run, args=(on_message, session_stop),
                                       name=f"chat-{self.ch.key}", daemon=True)
        rec.start()
        chat_thread.start()
        last_live_check = time.time()
        try:
            while not self.stop.is_set():
                self.stop.wait(1)
                now = time.time()
                with lock:
                    spike = det.check(now)
                if spike:
                    log.info("%s: chat spike x%.1f (%.1f/s vs %.1f/s)", self.ch.key,
                             spike.score, spike.rate, spike.baseline)
                    self.clipper.submit(self.ch, rec, spike)
                if int(now) % 30 == 0:
                    # Keep a little extra past the buffer for cuts still waiting.
                    rec.prune(now, self.cfg.buffer_s + self.cfg.pre_s + self.cfg.post_s)
                if not rec.alive():
                    if now - last_live_check > 20 and not is_live(self.ch.platform, self.ch.name):
                        log.info("%s went offline", self.ch.key)
                        return
                    last_live_check = now
                    rec.stop()
                    rec.start()
        finally:
            session_stop.set()
            rec.stop()


def run(cfg: Config) -> None:
    stop = threading.Event()
    engine = Engine(cfg.engine_url)
    while not engine.healthy():
        log.info("waiting for the engine at %s", cfg.engine_url)
        time.sleep(5)
    clipper = Clipper(cfg, engine)
    workers = [ChannelWorker(ch, cfg, clipper, stop) for ch in cfg.channels]
    for w in workers:
        w.start()
    log.info("watching %d channels", len(workers))
    if cfg.trends.enabled and clipper.telegram is not None:
        TrendsWorker(cfg.trends, clipper.telegram, cfg.work_dir, stop).start()
        log.info("searching viral GTA 6 videos every %d min", cfg.trends.every_min)
    try:
        while any(w.is_alive() for w in workers):
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()
