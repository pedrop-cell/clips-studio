"""Live clipper: spike detection, chat parsing, segment selection, posting caps.

Nothing here opens a socket or starts a process.
"""

import json
import subprocess
from pathlib import Path

import pytest

from live_clipper import config as lc_config
from live_clipper.chat import parse_privmsg, parse_pusher
from live_clipper.detector import SpikeDetector, message_weight
from live_clipper.engine import best_clip
from live_clipper.poster import Ledger, Limits, clean_tags, compose
from live_clipper.recorder import Recorder, segment_start, select

# ---- detector ---------------------------------------------------------------


def live(det, schedule, text_for=lambda t: "hello"):
    """Play chat second by second, checking each second like the runner does.

    `schedule(t)` is how many messages arrive in second t. Returns the spikes.
    """
    spikes = []
    for t in range(len(schedule)):
        n = schedule[t]
        for i in range(n):
            det.add(t + i / n * 0.9, text_for(t))
        if (s := det.check(t)) is not None:
            spikes.append(s)
    return spikes


def test_hype_messages_weigh_more():
    assert message_weight("boa noite") == 1.0
    assert message_weight("KEKW") == 2.0
    assert message_weight("kkkkkkk") == 2.0
    assert message_weight("o que???") == 2.0
    assert message_weight("CLIPA ISSO") == 2.0


def test_steady_chat_never_spikes():
    assert live(SpikeDetector(), [2] * 400) == []


def test_burst_against_own_baseline_spikes_with_onset_before_trigger():
    det = SpikeDetector(window_s=10, ratio=3.0, cooldown_s=90)
    spikes = live(det, [2] * 300 + [12] * 20, lambda t: "KEKW" if t >= 300 else "oi")
    assert len(spikes) == 1
    spike = spikes[0]
    assert spike.score >= 3
    assert 298 <= spike.onset <= spike.at <= 310


def test_quiet_channel_needs_min_rate():
    sched = [1 if t % 20 == 0 else 0 for t in range(200)] + [3]
    assert live(SpikeDetector(min_rate=1.0), sched) == []


def test_cooldown_blocks_a_second_spike():
    assert len(live(SpikeDetector(cooldown_s=90), [1] * 300 + [10] * 30)) == 1


def test_no_spike_during_warmup():
    assert live(SpikeDetector(warmup_s=60), [0] * 5 + [20] * 30) == []


# ---- chat parsing -----------------------------------------------------------


def test_twitch_privmsg_with_tags():
    line = "@badge-info=;color=#FF0000 :u!u@u.tmi.twitch.tv PRIVMSG #xqc :KEKW that car"
    assert parse_privmsg(line) == "KEKW that car"
    assert parse_privmsg(":tmi.twitch.tv 001 justinfan1 :Welcome") is None


def test_kick_pusher_chat_event():
    frame = json.dumps({"event": "App\\Events\\ChatMessageEvent",
                        "data": json.dumps({"content": "kkkkk", "sender": {"username": "a"}})})
    assert parse_pusher(frame) == "kkkkk"
    assert parse_pusher(json.dumps({"event": "pusher:connection_established", "data": "{}"})) is None
    assert parse_pusher("not json") is None


# ---- recorder ---------------------------------------------------------------


def segs(folder: Path, starts):
    out = []
    for s in starts:
        import time

        p = folder / (time.strftime("%Y%m%dT%H%M%S", time.gmtime(s)) + ".ts")
        p.write_bytes(b"x")
        out.append((float(s), p))
    return out


def test_segment_names_round_trip(tmp_path):
    (s,) = segs(tmp_path, [1_763_000_000])
    assert segment_start(s[1]) == 1_763_000_000


def test_select_covers_window_and_skips_the_open_segment(tmp_path):
    base = 1_763_000_000
    all_segs = segs(tmp_path, [base + 6 * i for i in range(10)])
    paths, offset = select(all_segs, base + 14, base + 30)
    assert [segment_start(p) - base for p in paths] == [12, 18, 24]
    assert offset == pytest.approx(2.0)
    # The last segment is still being written and is never used.
    paths, _ = select(all_segs, base + 50, base + 70)
    assert all(segment_start(p) - base < 54 for p in paths)


def test_select_nothing_recorded(tmp_path):
    assert select([], 0, 10) == ([], 0.0)


def test_cut_builds_a_stream_copy(tmp_path):
    base = 1_763_000_000
    segs(tmp_path, [base + 6 * i for i in range(6)])
    rec = Recorder("twitch", "x", tmp_path)
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"mp4")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    out = rec.cut(base + 3, base + 20, tmp_path / "out" / "c.mp4", run=fake_run)
    assert out and out.exists()
    assert "-c" in seen["cmd"] and "copy" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("-ss") + 1] == "3.00"


def test_prune_keeps_recent_and_the_open_segment(tmp_path):
    base = 1_763_000_000
    segs(tmp_path, [base + 6 * i for i in range(100)])
    Recorder("twitch", "x", tmp_path).prune(now=base + 600, keep_s=60)
    left = sorted(segment_start(p) for p in tmp_path.glob("*.ts"))
    assert left[0] >= base + 540 and left[-1] == base + 594


# ---- posting ----------------------------------------------------------------


def test_ledger_caps_and_gap(tmp_path):
    led = Ledger(tmp_path / "p.json", utc_offset_h=-3)
    limits = {"tiktok": Limits(per_day=2, min_gap_s=600), "youtube": Limits(per_day=5, min_gap_s=0)}
    now = 1_763_020_000.0
    assert led.allowed(["tiktok", "youtube"], limits, now) == ["tiktok", "youtube"]
    led.record(["tiktok", "youtube"], now)
    assert led.allowed(["tiktok", "youtube"], limits, now + 60) == ["youtube"]
    led.record(["tiktok"], now + 700)
    assert led.allowed(["tiktok"], limits, now + 1400) == []  # 2 today
    # Survives a restart.
    assert Ledger(tmp_path / "p.json").allowed(["tiktok"], limits, now + 1400) == []


def test_compose_credits_streamer_and_fits_tiktok_caption():
    clip = {"id": 1, "title": "Ele não acreditou no carro", "description": "Reação",
            "hashtags": ["#funny", "#GTA6"]}
    post = compose(clip, channel="Coringa", platform="twitch", source_url="https://www.twitch.tv/coringa",
                   hashtags=["GTA6", "GTAVI"], credit_template="Créditos: {channel} ({url})",
                   platforms=["tiktok", "youtube"], privacy={"tiktok": "PUBLIC_TO_EVERYONE", "youtube": "public"})
    assert post.tags == ["GTA6", "GTAVI", "funny"]
    assert "Créditos: Coringa" in post.description
    assert "Créditos: Coringa" in post.overrides["tiktok"]["title"]
    assert "#GTA6" in post.overrides["tiktok"]["title"]
    assert post.overrides["tiktok"]["privacy"] == "PUBLIC_TO_EVERYONE"
    assert post.overrides["youtube"] == {"privacy": "public"}


def test_clean_tags_dedupes_and_strips():
    assert clean_tags(["#GTA 6", "gta6"], ["GTA6", "#Vice-City"]) == ["GTA6", "ViceCity"]


def test_best_clip_needs_a_file():
    assert best_clip([{"id": 1, "score": 90}, {"id": 2, "score": 50, "path": "/a.mp4"}])["id"] == 2
    assert best_clip([]) is None


# ---- config -----------------------------------------------------------------


def test_example_config_loads():
    path = Path(__file__).resolve().parent.parent / "live_clipper" / "config.example.yaml"
    cfg = lc_config.load(path)
    assert any(c.name == "coringa" for c in cfg.channels)
    assert cfg.limits["tiktok"].per_day == 12
    assert cfg.engine_options["gaming"] is True


def test_config_rejects_unknown_platform(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("channels: ['youtube:someone']\n")
    with pytest.raises(ValueError):
        lc_config.load(p)


# ---- end to end, with fakes ---------------------------------------------------


class FakeEngine:
    def __init__(self, clip_path):
        self.clip_path = clip_path
        self.submitted = []
        self.published = []

    def submit_local(self, path, **kw):
        self.submitted.append((path, kw))
        return 7, "local_abc"

    def wait_job(self, job_id):
        return {"status": "done"}

    def clips(self, video_id):
        return [{"id": 3, "score": 80, "path": str(self.clip_path), "title": "Que isso",
                 "hashtags": ["#gta6"]}]

    def publish(self, clip_id, **kw):
        self.published.append((clip_id, kw))


class FakeRecorder:
    def covered_until(self):
        return 1e12

    def cut(self, t0, t1, out):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x")
        self.window = (t0, t1)
        return out


class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_clip(self, video, short, full):
        self.sent.append((video, short, full))


def make_cfg(tmp_path, **kw):
    from live_clipper.config import Channel, Config

    return Config(channels=[Channel("twitch", "coringa")], work_dir=tmp_path, pre_s=25, post_s=15,
                  telegram_token="t", telegram_chat_id="1", hashtags=["GTA6"],
                  engine_options=dict(lc_config.DEFAULT_ENGINE_OPTIONS), **kw)


def test_spike_becomes_a_telegram_delivery(tmp_path):
    from live_clipper.config import Channel
    from live_clipper.detector import Spike
    from live_clipper.runner import Clipper

    engine = FakeEngine(tmp_path / "clip.mp4")
    clipper = Clipper(make_cfg(tmp_path), engine=engine)
    clipper.telegram = FakeTelegram()
    rec = FakeRecorder()
    spike = Spike(at=1000.0, onset=995.0, rate=12, baseline=2, score=6)
    clipper.handle(Channel("twitch", "coringa"), rec, spike, sleep=lambda s: None)
    assert rec.window == (970.0, 1010.0)
    path, kw = engine.submitted[0]
    assert kw["options"]["gaming"] is True and kw["source_url"] == "https://www.twitch.tv/coringa"
    (video, short, full), = clipper.telegram.sent
    assert "Créditos: coringa" in full and "#GTA6" in full
    assert engine.published == []


def test_upload_post_mode_respects_caps(tmp_path):
    from live_clipper.config import Channel
    from live_clipper.detector import Spike
    from live_clipper.runner import Clipper

    engine = FakeEngine(tmp_path / "clip.mp4")
    cfg = make_cfg(tmp_path, mode="upload_post", platforms=["tiktok"],
                   limits={"tiktok": Limits(per_day=1, min_gap_s=0)})
    clipper = Clipper(cfg, engine=engine)
    assert clipper.telegram is None
    spike = Spike(at=1000.0, onset=995.0, rate=12, baseline=2, score=6)
    for _ in range(2):
        clipper.handle(Channel("twitch", "coringa"), FakeRecorder(), spike, sleep=lambda s: None)
    assert len(engine.published) == 1
