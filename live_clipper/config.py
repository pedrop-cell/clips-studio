"""The live clipper's YAML config. See live_clipper/config.example.yaml."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from live_clipper.poster import Limits
from live_clipper.trends import TrendsConfig
from live_clipper.vods import VodConfig

PLATFORMS = ("twitch", "kick", "youtube")
MODES = ("telegram", "upload_post")


@dataclass
class Channel:
    platform: str
    name: str
    label: str = ""  # how the credit names them; defaults to name

    @property
    def key(self) -> str:
        # Used as a folder name, so no ":" or "/" (Windows) from YouTube names.
        return re.sub(r"[^\w.@-]", "_", f"{self.platform}_{self.name}")


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YT_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def youtube_name(value: str) -> str:
    """The canonical YouTube name for a handle, channel id, or pasted link.

    "@handle" (lowercased) for a channel handle, the "UC..." id as-is (ids are
    case-sensitive), or "watch:<video id>" for a link to one live video.
    """
    v = value.strip()
    m = re.match(r"^(?:https?://)?(?:www\.|m\.)?(youtube\.com|youtu\.be)/(.*)$", v, re.I)
    if m:
        host, path = m.group(1).lower(), m.group(2)
        path, _, query = path.partition("?")
        parts = [p for p in path.split("/") if p]
        if host == "youtu.be" and parts and _YT_ID.match(parts[0]):
            return f"watch:{parts[0]}"
        if parts and parts[0] == "watch":
            vid = re.search(r"(?:^|&)v=([A-Za-z0-9_-]{11})", query)
            if vid:
                return f"watch:{vid.group(1)}"
        if len(parts) >= 2 and parts[0] == "live" and _YT_ID.match(parts[1]):
            return f"watch:{parts[1]}"
        if len(parts) >= 2 and parts[0] == "channel":
            v = parts[1]
        elif parts and parts[0].startswith("@"):
            v = parts[0]
        elif len(parts) >= 2 and parts[0] in ("c", "user"):
            v = "@" + parts[1]
        else:
            raise ValueError(f"não entendi este link do YouTube: {value}")
    if v.startswith("watch:"):
        return v
    if _YT_CHANNEL_ID.match(v):
        return v
    v = v.lstrip("@").lower()
    if not v or not re.match(r"^[\w.-]+$", v):
        raise ValueError(f"canal do YouTube inválido: {value}")
    return "@" + v


def parse_channel(entry) -> "Channel":
    """A config entry: "platform:name", a pasted channel link, or a dict."""
    if isinstance(entry, str):
        s = entry.strip()
        if re.match(r"^(?:https?://)?(?:www\.|m\.)?(youtube\.com|youtu\.be)/", s, re.I):
            entry = {"platform": "youtube", "name": s}
        elif m := re.match(r"^(?:https?://)?(?:www\.)?(twitch\.tv|kick\.com)/([^/?#]+)", s, re.I):
            entry = {"platform": m.group(1).split(".")[0].lower(), "name": m.group(2)}
        else:
            platform, _, name = s.partition(":")
            entry = {"platform": platform, "name": name}
    platform = str(entry["platform"]).lower().strip()
    raw_name = str(entry["name"]).strip()
    if platform not in PLATFORMS:
        raise ValueError(f"channel {raw_name}: platform must be one of {PLATFORMS}")
    name = youtube_name(raw_name) if platform == "youtube" else raw_name.lower()
    return Channel(platform=platform, name=name, label=str(entry.get("label") or ""))


@dataclass
class Detection:
    window_s: int = 10
    baseline_s: int = 300
    ratio: float = 3.0
    min_rate: float = 1.0
    warmup_s: int = 60
    cooldown_s: int = 90
    hype_weight: float = 1.0


@dataclass
class Config:
    channels: list[Channel]
    work_dir: Path = Path("/data/live")
    engine_url: str = "http://127.0.0.1:8765"
    buffer_s: int = 600
    pre_s: int = 25
    post_s: int = 15
    live_check_s: int = 60
    detection: Detection = field(default_factory=Detection)
    max_parallel_jobs: int = 2
    publish: bool = True
    # telegram: send each clip to a Telegram chat to post by hand (free).
    # upload_post: post straight to the platforms through Upload-Post (paid for TikTok).
    mode: str = "telegram"
    telegram_chat_id: str = ""
    telegram_token: str = ""
    platforms: list[str] = field(default_factory=lambda: ["tiktok", "youtube"])
    limits: dict[str, Limits] = field(default_factory=dict)
    privacy: dict[str, str] = field(default_factory=dict)
    hashtags: list[str] = field(default_factory=list)
    credit: str = "Créditos: {channel} ({url})"
    utc_offset_h: float = -3.0
    engine_options: dict = field(default_factory=dict)
    # Viral GTA 6 videos from other channels, sent to Telegram as ideas.
    trends: TrendsConfig = field(default_factory=TrendsConfig)
    vods: VodConfig = field(default_factory=VodConfig)


DEFAULT_ENGINE_OPTIONS = {
    "gaming": True,
    "captions": True,
    "max_clips": 1,
    "min_score": 0,
    "focus": "the strongest reaction or hype moment; start right before it happens",
}


def load(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text("utf-8")) or {}
    channels = []
    for c in raw.get("channels") or []:
        channels.append(parse_channel(c))
    if not channels:
        raise ValueError("no channels in the config")
    pub = raw.get("publish") or {}
    mode = str(pub.get("mode") or "telegram").lower()
    if mode not in MODES:
        raise ValueError(f"publish.mode must be one of {MODES}")
    tg = pub.get("telegram") or {}
    limits = {p: Limits(**v) for p, v in (pub.get("limits") or {}).items()}
    return Config(
        channels=channels,
        work_dir=Path(raw.get("work_dir") or "/data/live"),
        engine_url=raw.get("engine_url") or "http://127.0.0.1:8765",
        buffer_s=int(raw.get("buffer_s", 600)),
        pre_s=int(raw.get("pre_s", 25)),
        post_s=int(raw.get("post_s", 15)),
        live_check_s=int(raw.get("live_check_s", 60)),
        detection=Detection(**(raw.get("detection") or {})),
        max_parallel_jobs=int(raw.get("max_parallel_jobs", 2)),
        publish=bool(pub.get("enabled", True)),
        mode=mode,
        telegram_chat_id=str(tg.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")),
        # The token is a secret: from the environment only, never the YAML.
        telegram_token=os.environ.get(str(tg.get("token_env") or "TELEGRAM_BOT_TOKEN"), ""),
        platforms=list(pub.get("platforms") or ["tiktok", "youtube"]),
        limits=limits,
        privacy=dict(pub.get("privacy") or {}),
        hashtags=list(pub.get("hashtags") or []),
        credit=str(pub.get("credit") or "Créditos: {channel} ({url})"),
        utc_offset_h=float(raw.get("utc_offset_h", -3.0)),
        engine_options={**DEFAULT_ENGINE_OPTIONS, **(raw.get("engine_options") or {})},
        trends=TrendsConfig.from_raw(raw.get("trends")),
        vods=VodConfig.from_raw(raw.get("vods")),
    )
