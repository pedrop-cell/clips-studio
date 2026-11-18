"""The live clipper's YAML config. See live_clipper/config.example.yaml."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from live_clipper.poster import Limits
from live_clipper.trends import TrendsConfig

PLATFORMS = ("twitch", "kick")
MODES = ("telegram", "upload_post")


@dataclass
class Channel:
    platform: str
    name: str
    label: str = ""  # how the credit names them; defaults to name

    @property
    def key(self) -> str:
        return f"{self.platform}_{self.name}"


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
        if isinstance(c, str):
            platform, _, name = c.partition(":")
            c = {"platform": platform, "name": name}
        ch = Channel(platform=str(c["platform"]).lower(), name=str(c["name"]).lower().strip(),
                     label=str(c.get("label") or ""))
        if ch.platform not in PLATFORMS:
            raise ValueError(f"channel {ch.name}: platform must be one of {PLATFORMS}")
        channels.append(ch)
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
    )
