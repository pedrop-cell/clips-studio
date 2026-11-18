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
import sys
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
    if platform == "youtube":
        # channel is what config.youtube_name returns.
        if channel.startswith("watch:"):
            return f"https://www.youtube.com/watch?v={channel[6:]}"
        if channel.startswith("@"):
            return f"https://www.youtube.com/{channel}/live"
        return f"https://www.youtube.com/channel/{channel}/live"
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


def streamlink_cmd() -> list[str] | None:
    """streamlink, if installed. It drops Twitch's ad segments, which yt-dlp
    passes through: an ad break (a pre-roll on every connect, for an
    anonymous viewer) rewinds the timestamps and the segmenter then stalls."""
    exe = shutil.which("streamlink")
    if exe:
        return [exe]
    try:
        import importlib.util

        if importlib.util.find_spec("streamlink"):
            return [sys.executable, "-m", "streamlink"]
    except (ImportError, ValueError):
        pass
    return None


def ytdlp_cmd() -> list[str]:
    exe = shutil.which("yt-dlp")
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]


# YouTube clients to try in turn, for the live's HLS stream. None = yt-dlp's
# defaults. Which ones hand out a live HLS manifest without a PO token
# changes with YouTube; the others are fallbacks.
YT_CLIENTS = (None, "tv,web_safari", "web_embedded", "android_vr", "mweb")


class _FileLog:
    """A yt-dlp logger that keeps warnings and errors in a file."""

    def __init__(self, fh):
        self.fh = fh

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.fh.write(f"{msg}\n")

    def error(self, msg: str) -> None:
        self.fh.write(f"{msg}\n")


class _Silent:
    def debug(self, msg: str) -> None:
        pass

    info = warning = error = debug


def _has(f: dict, kind: str) -> bool:
    return f.get(kind) != "none"  # None means "not stated", which HLS often is


def pick_muxed(formats: list[dict], max_height: int = 1080) -> dict | None:
    """The best format with both video and audio in one stream, ideally <= max_height."""
    av = [f for f in formats if f.get("url") and _has(f, "vcodec") and _has(f, "acodec")
          and (f.get("height") or f.get("vcodec") not in (None, "none"))]
    if not av:
        return None

    def rank(f: dict) -> tuple:
        h = f.get("height") or 0
        return (h <= max_height, h if h <= max_height else -h, f.get("tbr") or 0)

    return max(av, key=rank)


def _describe(formats: list[dict]) -> str:
    out = []
    for f in formats[-12:]:
        av = ("v" if _has(f, "vcodec") else "") + ("a" if _has(f, "acodec") else "")
        out.append(f"{f.get('format_id')}:{f.get('protocol')}:{f.get('height') or '?'}p:{av or '-'}")
    return ", ".join(out) or "none"


def youtube_stream(channel: str, log_file: Path,
                   extract: Callable[[str, dict], dict] | None = None) -> tuple[str, dict, str] | None:
    """(stream url, http headers, description) of a YouTube live, or None.

    Tries yt-dlp's clients in turn and takes the first one that gives a
    format with video and audio together, which ffmpeg then reads directly.
    What each client gave is written to `log_file`, for last_error.
    """
    url = live_url("youtube", channel)
    with open(log_file, "w", encoding="utf-8") as fh:
        def default_extract(u: str, opts: dict) -> dict:
            import yt_dlp

            with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True, "logger": _FileLog(fh),
                                   **opts}) as ydl:
                return ydl.extract_info(u, download=False)

        run = extract or default_extract
        for client in YT_CLIENTS:
            opts = {"extractor_args": {"youtube": {"player_client": client.split(",")}}} if client else {}
            name = client or "default"
            try:
                info = run(url, opts) or {}
            except Exception as e:
                fh.write(f"[{name}] {str(e)[:200]}\n")
                continue
            formats = info.get("formats") or []
            best = pick_muxed(formats)
            if best:
                fh.write(f"[{name}] using {best.get('format_id')} ({best.get('height')}p)\n")
                return best["url"], dict(best.get("http_headers") or {}), f"{name} {best.get('format_id')}"
            fh.write(f"[{name}] no video+audio format; got {_describe(formats)}\n")
    return None


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
        self._dl_log = None
        self._ff_log = None
        self.started_at = 0.0

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        sl = streamlink_cmd() if self.platform == "twitch" else None
        if sl:
            dl_cmd = [*sl, "--loglevel", "warning", "--twitch-disable-ads",
                      "--stdout", live_url(self.platform, self.channel), "1080p60,1080p,best"]
        else:
            dl_cmd = [*ytdlp_cmd(), "--quiet", "--no-part", "-f", self.quality, "-o", "-"]
            if self.platform == "kick":
                dl_cmd += ["--impersonate", "chrome"]
            dl_cmd.append(live_url(self.platform, self.channel))
        ff_cmd = [
            ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
            "-f", "segment", "-segment_time", str(SEGMENT_S), "-reset_timestamps", "1",
            "-strftime", "1", str(self.folder / f"{SEGMENT_FMT}.ts"),
        ]
        # "UTC0" is read the same by glibc and the Windows C runtime, so segment
        # names are UTC wall-clock on a Windows PC too.
        env = {**os.environ, "TZ": "UTC0"}
        if self.platform == "youtube":
            self._start_youtube(ff_cmd, env)
            return
        # yt-dlp's errors go to a file next to the buffer, so a recorder that
        # keeps dying can say why (last_error).
        self._close_log()
        self._dl_log = open(self.log_path, "wb")  # closed in stop()
        self._dl = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE, stderr=self._dl_log)
        self.started_at = time.time()
        self._ff = subprocess.Popen(ff_cmd, stdin=self._dl.stdout, env=env,
                                    stdout=subprocess.DEVNULL, stderr=self._open_ff_log())
        if self._dl.stdout:
            self._dl.stdout.close()  # ffmpeg owns the read end now
        log.info("recording %s/%s into %s", self.platform, self.channel, self.folder)

    def _start_youtube(self, ff_cmd: list[str], env: dict) -> None:
        """YouTube: yt-dlp finds the live's stream, ffmpeg reads it itself."""
        self._close_log()
        self.started_at = time.time()
        found = youtube_stream(self.channel, self.log_path)
        if not found:
            self._ff = None
            return
        url, headers, desc = found
        i = ff_cmd.index("pipe:0")
        hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        ff_cmd[i - 1:i + 1] = [*(["-headers", hdr] if hdr else []), "-i", url]
        self._ff = subprocess.Popen(ff_cmd, stdin=subprocess.DEVNULL, env=env,
                                    stdout=subprocess.DEVNULL, stderr=self._open_ff_log())
        log.info("recording %s/%s (%s) into %s", self.platform, self.channel, desc, self.folder)

    def alive(self) -> bool:
        if not (self._ff and self._ff.poll() is None):
            return False
        return self.platform == "youtube" or bool(self._dl and self._dl.poll() is None)

    def stop(self) -> None:
        for p in (self._dl, self._ff):
            if p and p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
        self._dl = self._ff = None
        self._close_log()

    @property
    def log_path(self) -> Path:
        return self.folder / "yt-dlp.log"

    @property
    def ff_log_path(self) -> Path:
        return self.folder / "ffmpeg.log"

    def _open_ff_log(self):
        # A file, never a pipe nobody reads: on Windows a pipe fills after a
        # few KB of ffmpeg complaints (Twitch ad breaks make plenty), and
        # ffmpeg then blocks for good, alive but writing no more segments.
        self._ff_log = open(self.ff_log_path, "wb")  # closed in stop()
        return self._ff_log

    def _close_log(self) -> None:
        for attr in ("_dl_log", "_ff_log"):
            fh = getattr(self, attr, None)
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
                setattr(self, attr, None)

    def last_error(self, max_chars: int = 1500) -> str:
        """The tail of what yt-dlp (and ffmpeg) printed before dying."""
        parts = []
        try:
            parts.append(self.log_path.read_text("utf-8", "replace").strip())
        except OSError:
            pass
        try:
            parts.append(self.ff_log_path.read_text("utf-8", "replace").strip()[-max_chars // 2:])
        except OSError:
            pass
        text = " | ".join(p for p in parts if p)
        return text[-max_chars:]

    def prune(self, now: float | None = None, keep_s: int | None = None) -> None:
        horizon = (now or time.time()) - (keep_s or self.buffer_s)
        for start, path in list_segments(self.folder)[:-1]:
            if start < horizon:
                path.unlink(missing_ok=True)

    def describe(self) -> str:
        """One line on the buffer's state, for a cut that found nothing."""
        segs = list_segments(self.folder)
        last = time.strftime("%H:%M:%S", time.gmtime(segs[-1][0])) + " UTC" if segs else "-"
        state = "gravando" if self.alive() else "parado"
        return f"{state}, {len(segs)} pedaços no buffer, último {last}"

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

    # An offline channel is an ERROR to yt-dlp; to us it is the normal case,
    # so nothing is printed (it filled Pedro's window every minute).
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Silent()}
    if platform == "kick":
        from live_clipper.chat import impersonation

        opts.update(impersonation())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url(platform, channel), download=False)
    except Exception:  # offline channels raise; so do network hiccups
        return False
    return live_from_info(platform, info)


def live_from_info(platform: str, info: dict | None) -> bool:
    if not info:
        return False
    if platform == "youtube":
        # An offline channel's /live can resolve to its page, a past stream or
        # an upcoming one; only a stream that is on air now counts.
        return info.get("live_status") == "is_live" or info.get("is_live") is True
    return bool(info.get("is_live", True))


def youtube_live_video_id(channel: str) -> str | None:
    """The id of the video a YouTube channel is streaming now, or None."""
    if channel.startswith("watch:"):
        return channel[6:]
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "logger": _Silent()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(live_url("youtube", channel), download=False)
    except Exception:
        return None
    return info.get("id") if live_from_info("youtube", info) else None
