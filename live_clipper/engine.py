"""The engine's local API, as the live clipper uses it.

The engine only answers requests addressed to 127.0.0.1 or localhost (see
docs/API.md, Security), so the clipper runs on the same host or in the same
network namespace (docker-compose.live.yml does the latter).

Stdlib HTTP, so it imports on the light CI runner.
"""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable


class EngineError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Engine:
    def __init__(self, base_url: str = "http://127.0.0.1:8765",
                 sleep: Callable[[float], None] = time.sleep):
        self.base = base_url.rstrip("/")
        self._sleep = sleep

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float = 60):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            raise EngineError(f"{method} {path}: HTTP {e.code} {detail}", e.code) from e
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"{method} {path}: {e}") from e
        return json.loads(raw) if raw else None

    def healthy(self) -> bool:
        try:
            return bool((self._call("GET", "/health", timeout=5) or {}).get("ok"))
        except EngineError:
            return False

    def submit_local(self, path: str, *, title: str, channel: str, platform: str,
                     source_url: str, options: dict, queue_wait_s: float = 1800,
                     on_full: Callable[[], None] | None = None) -> tuple[int, str]:
        body = {"path": path, "title": title, "channel": channel, "platform": platform,
                "source_url": source_url, **options}
        # The app's queue holds 5 videos at most and answers 409 when full; a
        # busy night fills it, so wait for room instead of dropping the clip.
        deadline = time.monotonic() + queue_wait_s
        while True:
            try:
                res = self._call("POST", "/videos/local", body)
                break
            except EngineError as e:
                if e.status != 409 or time.monotonic() > deadline:
                    raise
                if on_full:
                    on_full()
                self._sleep(30)
        if not res or res.get("job_id") is None:
            raise EngineError(f"engine did not queue {path}: {res}")
        return int(res["job_id"]), str(res["video_id"])

    def wait_job(self, job_id: int, timeout_s: float = 1800, every_s: float = 5,
                 on_wait: Callable[[dict, float], None] | None = None,
                 report_every_s: float = 120) -> dict:
        """Poll a job until done or failed. `on_wait(job, waited_s)` is called
        every `report_every_s` meanwhile, so a slow job isn't silent."""
        start = time.monotonic()
        deadline = start + timeout_s
        next_report = start + report_every_s
        while True:
            job = self._call("GET", f"/jobs/{job_id}")
            if job and job.get("status") in ("done", "failed"):
                return job
            now = time.monotonic()
            if on_wait and job and now >= next_report:
                on_wait(job, now - start)
                next_report = now + report_every_s
            if now > deadline:
                raise EngineError(f"job {job_id} still {job and job.get('status')} after {timeout_s:.0f}s")
            self._sleep(every_s)

    def resume_queue(self) -> bool:
        """Turn the app's queue on. It starts stopped ("press Start queue"),
        which left clips waiting until someone pressed it by hand."""
        try:
            self._call("POST", "/queue/resume", {}, timeout=10)
            return True
        except EngineError:
            return False

    def queue_paused(self) -> bool:
        try:
            return bool((self._call("GET", "/queue", timeout=10) or {}).get("paused"))
        except EngineError:
            return False

    def download_clip(self, clip_id: int, dest, timeout: float = 300):
        """Save a rendered clip through the app's GET /media/{id}.

        The Microsoft Store app's files live in its own virtualised AppData
        (a Store app's writes there are redirected), so the path it reports
        does not exist for other programs; asking the app for the bytes works
        wherever it keeps them.
        """
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(f"{self.base}/media/{int(clip_id)}")
        tmp = dest.with_suffix(".part")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as fh:
                while chunk := r.read(1 << 20):
                    fh.write(chunk)
        except urllib.error.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise EngineError(f"GET /media/{clip_id}: HTTP {e.code}", e.code) from e
        except (urllib.error.URLError, OSError) as e:
            tmp.unlink(missing_ok=True)
            raise EngineError(f"GET /media/{clip_id}: {e}") from e
        tmp.replace(dest)
        return dest

    def clips(self, video_id: str) -> list[dict]:
        return self._call("GET", f"/videos/{video_id}/clips") or []

    def publish(self, clip_id: int, *, platforms: list[str], title: str, description: str,
                tags: list[str], overrides: dict) -> dict:
        return self._call("POST", f"/uploadpost/clips/{clip_id}/publish", {
            "platforms": platforms, "title": title, "description": description,
            "tags": tags, "overrides": overrides,
        }, timeout=300) or {}


def best_clip(clips: list[dict]) -> dict | None:
    """The highest-scoring clip that has a rendered file."""
    ready = [c for c in clips if c.get("path")]
    return max(ready, key=lambda c: c.get("score") or 0) if ready else None
