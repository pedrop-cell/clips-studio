"""A rolling recording of a live stream, and cutting moments out of it.

`yt-dlp -o - <live url> | ffmpeg -c copy -f segment` writes the stream as a
run of short .ts files named by the UTC wall-clock second each one started.
Chat timestamps are wall-clock too, so a spike at 01:23:45 maps straight to
the segments covering it. Nothing is re-encoded here: the cut is a stream
copy, and the engine encodes the clip once when it makes the Short.

Only the last `buffer_s` seconds are kept on disk.
"""

import calendar
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("live_clipper.recorder")

SEGMENT_FMT = "%Y%m%dT%H%M%S"
SEGMENT_S = 6


def live_url(platform: str, channel: str) -> str:
    if platform == "twitch":
        return f"https://www.twitch.tv/{channel}"
    if platform == "kick":
        return f"https://kick.com/{channel}"
    raise ValueError(f"unsupported platform {platform!r}")


def segment_start(path: Path) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(path.stem, SEGMENT_FMT)))
    except ValueError:
        return None


def list_segments(folder: Path) -> list[tuple[float, Path]]:
    out = []
    for p in folder.glob("*.ts"):
        t = segment_start(p)
        if t is not None:
            out.append((t, p))
    out.sort()
    return out


def select(segments: list[tuple[float, Path]], t0: float, t1: float) -> tuple[list[Path], float]:
    """The segments that cover [t0, t1], and t0's offset into the first one.

    A segment's end is the next one's start; the last (still being written)
    one is never included, since its file is incomplete.
    """
    chosen: list[Path] = []
    first_start = None
    for (start, path), (nxt, _) in zip(segments, segments[1:], strict=False):
        if nxt <= t0 or start >= t1:
            continue
        if first_start is None:
            first_start = start
        chosen.append(path)
    if first_start is None:
        return [], 0.0
    return chosen, max(0.0, t0 - first_start)


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else ["python", "-m", "yt_dlp"]


class Recorder:
    def __init__(self, platform: str, channel: str, folder: Path, buffer_s: int = 600,
                 quality: str = "best[height<=1080]/best"):
        self.platform = platform
        self.channel = channel
        self.folder = Path(folder)
        self.buffer_s = buffer_s
        self.quality = quality
        self._dl: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        dl_cmd = [*ytdlp_cmd(), "--quiet", "--no-warnings", "--no-part", "-f", self.quality, "-o", "-"]
        if self.platform == "kick":
            dl_cmd += ["--impersonate", "chrome"]
        dl_cmd.append(live_url(self.platform, self.channel))
        ff_cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_S), "-reset_timestamps", "1",
            "-strftime", "1", str(self.folder / f"{SEGMENT_FMT}.ts"),
        ]
        env = {**os.environ, "TZ": "UTC"}
        self._dl = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self._ff = subprocess.Popen(ff_cmd, stdin=self._dl.stdout, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if self._dl.stdout:
            self._dl.stdout.close()  # ffmpeg owns the read end now
        log.info("recording %s/%s into %s", self.platform, self.channel, self.folder)

    def alive(self) -> bool:
        return bool(self._ff and self._ff.poll() is None and self._dl and self._dl.poll() is None)

    def stop(self) -> None:
        for p in (self._dl, self._ff):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        self._dl = self._ff = None

    def prune(self, now: float | None = None, keep_s: int | None = None) -> None:
        horizon = (now or time.time()) - (keep_s or self.buffer_s)
        for start, path in list_segments(self.folder)[:-1]:
            if start < horizon:
                path.unlink(missing_ok=True)

    def covered_until(self) -> float:
        """Wall-clock time up to which complete segments exist."""
        segs = list_segments(self.folder)
        return segs[-1][0] if segs else 0.0

    def cut(self, t0: float, t1: float, out: Path,
            run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> Path | None:
        paths, offset = select(list_segments(self.folder), t0, t1)
        if not paths:
            return None
        out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", "concat:" + "|".join(str(p) for p in paths),
            "-ss", f"{offset:.2f}", "-t", f"{t1 - t0:.2f}",
            "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(out),
        ]
        res = run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not out.exists():
            log.error("cut failed for %s: %s", out.name, (res.stderr or "")[-400:])
            return None
        return out


def is_live(platform: str, channel: str) -> bool:
    """True if the channel is streaming now. One yt-dlp metadata request."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if platform == "kick":
        from sources.kick import _impersonation

        opts.update(_impersonation())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url(platform, channel), download=False)
    except Exception:  # offline channels raise; so do network hiccups
        return False
    return bool(info and info.get("is_live", True))
