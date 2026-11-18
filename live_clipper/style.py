"""The look of every clip sent to Telegram: a hook title on the first frame.

Short videos are kept or swiped away in their first 1-3 seconds, and most
start with the sound off, so the clip opens with big text saying what is
about to happen ("ALANZOKA NÃO ACREDITOU"), shown for the first seconds.
It is burned here with ffmpeg (the same one the recorder uses) rather than
by the app, because the app only adds a hook when a clip is edited by hand.

The text is an ASS subtitle, so the font is found by name and nothing in it
needs filter escaping; ffmpeg runs inside the clip's folder and gets the
file by its bare name, which avoids the "C:" escaping Windows paths need.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("live_clipper")


@dataclass
class ClipStyle:
    hook: bool = True            # big title at the top in the first seconds
    hook_s: float = 3.0          # how long it stays on screen
    hook_text: str = "{title}"   # {title} = the clip's title, {channel} = streamer
    hook_font: str = "Impact"
    hook_size: int = 92          # at 1080x1920
    hook_color: str = "#FFFFFF"
    hook_top: int = 230          # px from the top: clear of TikTok's top bar
    outro: bool = False          # the app's 2.9s end card: off, it breaks the loop

    @classmethod
    def from_raw(cls, raw: dict | None) -> "ClipStyle":
        raw = raw or {}
        known = {k: raw[k] for k in cls.__dataclass_fields__ if k in raw}
        return cls(**known)


# Letters (accents too), digits, spaces and plain punctuation. Emoji and
# symbols are dropped: Impact has no glyphs for them and they burn as boxes.
_KEEP = re.compile(r"[^\w\s!?.,:;'\"%$&()+\-]", re.UNICODE)
LINE_CHARS = 18
MAX_LINES = 3


def hook_text(template: str, title: str, channel: str) -> str:
    """The hook as it is burned: uppercase, emoji-free, at most 3 short lines."""
    text = template.format(title=(title or "").strip(), channel=channel or "")
    text = _KEEP.sub("", text)
    text = " ".join(text.split()).upper()
    if not text:
        text = f"{channel} AO VIVO".upper().strip()
    lines: list[str] = []
    for word in text.split():
        if lines and len(lines[-1]) + 1 + len(word) <= LINE_CHARS:
            lines[-1] += " " + word
        else:
            lines.append(word)
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
        lines[-1] = lines[-1].rstrip(".,:;") + "..."
    return "\n".join(lines)


def _ass_color(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        h = "FFFFFF"
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_time(s: float) -> str:
    cs = int(round(max(0.0, s) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def hook_ass(text: str, style: ClipStyle) -> str:
    # ASS reads { } as override tags and \ as escapes; the text is made of
    # neither after hook_text, but strip them anyway so a title can't break it.
    body = text.replace("\\", "").replace("{", "").replace("}", "").replace("\n", "\\N")
    fade = "{\\fad(0,250)}"
    return (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Hook,{style.hook_font},{int(style.hook_size)},{_ass_color(style.hook_color)},"
        "&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,9,3,8,60,60,"
        f"{int(style.hook_top)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,{_ass_time(0)},{_ass_time(style.hook_s)},Hook,,0,0,0,,{fade}{body}\n"
    )


def ffmpeg_bin() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def with_hook(video: Path, title: str, channel: str, style: ClipStyle, out_dir: Path) -> Path:
    """`video` with the hook burned in (a new file in `out_dir`), or `video`
    itself when the hook is off or ffmpeg can't do it; a clip is never lost
    to its decoration."""
    if not style.hook or style.hook_s <= 0:
        return video
    text = hook_text(style.hook_text, title, channel)
    folder = Path(out_dir)
    folder.mkdir(parents=True, exist_ok=True)
    ass = folder / f"{video.stem}_hook.ass"
    out = folder / f"{video.stem}_hook.mp4"
    if out.is_file() and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    ass.write_text(hook_ass(text, style), "utf-8")
    cmd = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(video.resolve()),
           "-vf", f"subtitles={ass.name}", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
           out.name]
    try:
        r = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("hook text not added to %s (%s); sending it without", video.name, e)
        return video
    if r.returncode != 0 or not out.is_file():
        log.warning("hook text not added to %s (ffmpeg: %s); sending it without",
                    video.name, (r.stderr or "").strip()[-300:])
        out.unlink(missing_ok=True)
        return video
    return out
