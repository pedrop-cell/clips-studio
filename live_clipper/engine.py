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
    pass


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
            raise EngineError(f"{method} {path}: HTTP {e.code} {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise EngineError(f"{method} {path}: {e}") from e
        return json.loads(raw) if raw else None

    def healthy(self) -> bool:
        try:
            return bool((self._call("GET", "/health", timeout=5) or {}).get("ok"))
        except EngineError:
            return False

    def submit_local(self, path: str, *, title: str, channel: str, platform: str,
                     source_url: str, options: dict) -> tuple[int, str]:
        body = {"path": path, "title": title, "channel": channel, "platform": platform,
                "source_url": source_url, **options}
        res = self._call("POST", "/videos/local", body)
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

    def queue_paused(self) -> bool:
        try:
            return bool((self._call("GET", "/queue", timeout=10) or {}).get("paused"))
        except EngineError:
            return False

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
