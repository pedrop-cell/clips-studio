"""Live clipper: spike detection, chat parsing, segment selection, posting caps.

Nothing here opens a socket or starts a process.
"""

import json
import subprocess
import threading
from pathlib import Path

import pytest

from live_clipper import chat
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
    p.write_text("channels: ['facebook:someone']\n")
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

    def download_clip(self, clip_id, dest):
        self.downloaded = [*getattr(self, "downloaded", []), clip_id]
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"mp4")
        return Path(dest)

    def wait_job(self, job_id, **kw):
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


# ---- Windows setup helpers ---------------------------------------------------


def test_envfile_round_trip_never_overrides(tmp_path, monkeypatch):
    from live_clipper import envfile

    p = tmp_path / "clipador.env"
    envfile.save(p, {"TELEGRAM_BOT_TOKEN": "1:abc", "TELEGRAM_CHAT_ID": "42"})
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "7")
    envfile.load(p)
    import os

    assert os.environ["TELEGRAM_BOT_TOKEN"] == "1:abc"
    assert os.environ["TELEGRAM_CHAT_ID"] == "7"  # an explicit env var wins
    envfile.load(tmp_path / "missing.env")  # no file is fine


def test_setup_writes_config_with_local_work_dir(tmp_path):
    from live_clipper.setup import write_config

    cfg = lc_config.load(write_config(tmp_path))
    assert cfg.work_dir == (tmp_path / "dados").resolve()
    assert cfg.mode == "telegram"


def test_setup_waits_for_the_first_message():
    from live_clipper.setup import wait_chat_id

    answers = iter([None, None, "123"])
    assert wait_chat_id("t", sleep=lambda s: None, find=lambda t: next(answers)) == "123"


def test_setup_cleans_pasted_tokens(tmp_path):
    from live_clipper.setup import clean_token, token_from_file

    tok = "123456789:" + "A" * 35
    assert clean_token(f'  TELEGRAM_BOT_TOKEN="{tok}" ') == tok
    (tmp_path / "token.txt").write_text(tok + "\n", "utf-8")
    assert token_from_file(tmp_path) == tok
    assert token_from_file(tmp_path / "nope") == ""


def test_setup_rejects_a_bad_token():
    from live_clipper.setup import ask_token

    answers = iter(["oi", "123456789:" + "A" * 35])
    assert ask_token(ask=lambda prompt: next(answers)).startswith("123456789:")


def test_find_chat_id_reads_latest_message(monkeypatch):
    import io

    from live_clipper import notify

    body = {"ok": True, "result": [{"message": {"chat": {"id": 1}}}, {"message": {"chat": {"id": 99}}}]}
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(json.dumps(body).encode()))
    assert notify.find_chat_id("t") == "99"


# ---- YouTube ---------------------------------------------------------------------


@pytest.mark.parametrize("entry,name", [
    ("youtube:Mount", "@mount"),
    ("youtube:@Mount", "@mount"),
    ("https://www.youtube.com/@Mount", "@mount"),
    ("https://www.youtube.com/@Mount/live", "@mount"),
    ("youtube.com/@mount/streams", "@mount"),
    ("https://www.youtube.com/channel/UCabcdefghijklmnopqrstUV", "UCabcdefghijklmnopqrstUV"),
    ("https://www.youtube.com/watch?v=abcdefghijk&t=10", "watch:abcdefghijk"),
    ("https://youtu.be/abcdefghijk", "watch:abcdefghijk"),
    ("https://www.youtube.com/live/abcdefghijk?si=x", "watch:abcdefghijk"),
])
def test_youtube_channel_entries(entry, name):
    ch = lc_config.parse_channel(entry)
    assert (ch.platform, ch.name) == ("youtube", name)
    assert ":" not in ch.key and "/" not in ch.key


def test_pasted_twitch_and_kick_links():
    assert lc_config.parse_channel("https://www.twitch.tv/Coringa").name == "coringa"
    assert lc_config.parse_channel("https://kick.com/n3on").platform == "kick"


def test_youtube_live_urls():
    from live_clipper.recorder import live_url

    assert live_url("youtube", "@mount") == "https://www.youtube.com/@mount/live"
    assert live_url("youtube", "UCabcdefghijklmnopqrstUV").endswith("/channel/UCabcdefghijklmnopqrstUV/live")
    assert live_url("youtube", "watch:abcdefghijk") == "https://www.youtube.com/watch?v=abcdefghijk"


def test_youtube_only_on_air_counts_as_live():
    from live_clipper.recorder import live_from_info

    assert live_from_info("youtube", {"live_status": "is_live"})
    assert not live_from_info("youtube", {"live_status": "is_upcoming"})
    assert not live_from_info("youtube", {"live_status": "was_live"})
    assert not live_from_info("youtube", {"_type": "playlist"})
    assert live_from_info("twitch", {"id": "x"})


YT_PAGE = (
    '<script>ytcfg.set({"INNERTUBE_API_KEY":"KEY123","INNERTUBE_CLIENT_VERSION":"2.20261101.01.00"});'
    'var ytInitialData = {"contents":{"liveChatRenderer":{"continuations":[{"invalidationContinuationData":'
    '{"invalidationId":{"objectSource":1},"timeoutMs":10000,"continuation":"CONT0"}}]}}};</script>'
)


def test_youtube_chat_page_gives_key_and_continuation():
    assert chat.parse_youtube_chat_page(YT_PAGE) == ("KEY123", "2.20261101.01.00", "CONT0")
    with pytest.raises(OSError):
        chat.parse_youtube_chat_page("<html>no chat</html>")


def _yt_answer(cont="CONT1"):
    return {"continuationContents": {"liveChatContinuation": {
        "continuations": [{"timedContinuationData": {"timeoutMs": 1000, "continuation": cont}}] if cont else [],
        "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "timestampUsec": "1763470000000000",
                "message": {"runs": [{"text": "KKKKKK "}, {"emoji": {"emojiId": "x", "shortcuts": [":joy:"]}}]},
            }}}},
            {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
                "timestampUsec": "1763470001000000", "message": {"runs": [{"text": "CLIPA"}]},
            }}}},
            {"addChatItemAction": {"item": {"liveChatViewerEngagementMessageRenderer": {}}}},
            {"markChatItemAsDeletedAction": {}},
        ],
    }}}


def test_youtube_chat_answer_parses_messages_and_next_poll():
    msgs, cont, wait = chat.parse_youtube_chat(_yt_answer())
    assert msgs == [(1763470000.0, "KKKKKK :joy:"), (1763470001.0, "CLIPA")]
    assert cont == "CONT1" and wait == 1.0
    assert chat.parse_youtube_chat(_yt_answer(cont=None))[1] is None


def test_youtube_chat_reader_polls_until_chat_ends():
    calls = []

    def http(url, body=None, timeout=20):
        calls.append((url, body))
        if body is None:
            assert "v=abcdefghijk" in url
            return YT_PAGE
        assert "key=KEY123" in url and body["continuation"] in ("CONT0", "CONT1")
        return json.dumps(_yt_answer(cont="CONT1" if body["continuation"] == "CONT0" else None))

    reader = chat.reader_for("youtube", "@mount", http=http, video_id=lambda c: "abcdefghijk")
    got = []
    stop = threading.Event()
    with pytest.raises(OSError, match="chat ended"):
        reader._session(lambda ts, text: got.append(text), stop)
    assert got == ["KKKKKK :joy:", "CLIPA"] * 2
    assert len(calls) == 3


def test_recorder_reports_why_yt_dlp_died(tmp_path):
    rec = Recorder("youtube", "@mount", tmp_path)
    assert rec.last_error() == ""
    rec.log_path.write_text("ERROR: [youtube] abc: Requested format is not available\n")
    assert "Requested format is not available" in rec.last_error()


def test_pick_muxed_prefers_1080_with_audio():
    from live_clipper.recorder import pick_muxed

    fmts = [
        {"format_id": "137", "url": "u", "height": 1080, "vcodec": "avc1", "acodec": "none"},
        {"format_id": "140", "url": "u", "vcodec": "none", "acodec": "mp4a"},
        {"format_id": "94", "url": "u94", "height": 480, "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "301", "url": "u301", "height": 1080, "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "315", "url": "u315", "height": 2160, "vcodec": "vp9", "acodec": "mp4a"},
    ]
    assert pick_muxed(fmts)["format_id"] == "301"
    assert pick_muxed(fmts[:2]) is None


def test_youtube_stream_falls_back_to_the_next_client(tmp_path):
    from live_clipper.recorder import youtube_stream

    tried = []

    def extract(url, opts):
        client = opts.get("extractor_args", {}).get("youtube", {}).get("player_client")
        tried.append(client)
        if client is None:
            return {"formats": [{"format_id": "137", "url": "u", "height": 1080, "vcodec": "avc1", "acodec": "none"}]}
        if client == ["tv", "web_safari"]:
            raise RuntimeError("Sign in to confirm")
        return {"formats": [{"format_id": "95", "url": "hls95", "height": 720, "vcodec": "avc1",
                             "acodec": "mp4a", "http_headers": {"User-Agent": "x"}}]}

    log_file = tmp_path / "yt-dlp.log"
    assert youtube_stream("@mount", log_file, extract=extract) == ("hls95", {"User-Agent": "x"}, "web_embedded 95")
    assert tried == [None, ["tv", "web_safari"], ["web_embedded"]]
    text = log_file.read_text()
    assert "no video+audio format; got 137:None:1080p:v" in text and "Sign in" in text


def test_youtube_recorder_feeds_ffmpeg_the_stream_url(tmp_path, monkeypatch):
    from live_clipper import recorder as rmod

    monkeypatch.setattr(rmod, "youtube_stream", lambda ch, lf: ("https://hls/x.m3u8", {"User-Agent": "UA"}, "default 301"))
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            self.stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(rmod.subprocess, "Popen", FakePopen)
    rec = rmod.Recorder("youtube", "@mount", tmp_path)
    rec.start()
    cmd = seen["cmd"]
    assert cmd[cmd.index("-i") + 1] == "https://hls/x.m3u8"
    assert cmd[cmd.index("-headers") + 1] == "User-Agent: UA\r\n"
    assert rec.alive()


def test_wait_job_reports_while_slow():
    from live_clipper.engine import Engine

    clock = [0.0]
    states = iter(["queued", "running", "running", "done"])
    eng = Engine(sleep=lambda s: clock.__setitem__(0, clock[0] + 100))
    eng._call = lambda m, p, b=None, timeout=60: {"status": next(states)}
    seen = []
    import live_clipper.engine as emod

    real = emod.time.monotonic
    emod.time.monotonic = lambda: clock[0]
    try:
        job = eng.wait_job(1, on_wait=lambda j, w: seen.append((j["status"], w)), report_every_s=150)
    finally:
        emod.time.monotonic = real
    assert job["status"] == "done"
    assert seen == [("running", 200.0)]


def test_ffmpeg_errors_go_to_a_file_not_a_pipe(tmp_path, monkeypatch):
    from live_clipper import recorder as rmod

    calls = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            calls.append(kw)
            self.stdout = None
            self.stderr = None

        def poll(self):
            return None

    monkeypatch.setattr(rmod.subprocess, "Popen", FakePopen)
    rec = rmod.Recorder("twitch", "alanzoka", tmp_path)
    rec.start()
    assert all(kw.get("stderr") is not rmod.subprocess.PIPE for kw in calls)
    rec._ff_log.write(b"[mpegts] Packet corrupt\n")
    rec._ff_log.flush()
    assert "Packet corrupt" in rec.last_error()
    assert "0 pedaços" in rec.describe()


def test_submit_waits_for_room_in_a_full_queue_and_resume_calls_the_api():
    from live_clipper.engine import Engine, EngineError

    calls = []
    answers = [EngineError("full", 409), EngineError("full", 409), {"job_id": 9, "video_id": "local_x"}]

    def fake_call(method, path, body=None, timeout=60):
        calls.append((method, path))
        if path == "/videos/local":
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return {"paused": False}

    eng = Engine(sleep=lambda s: None)
    eng._call = fake_call
    fulls = []
    assert eng.submit_local("c.mp4", title="t", channel="c", platform="twitch", source_url="u",
                            options={}, on_full=lambda: fulls.append(1)) == (9, "local_x")
    assert len(fulls) == 2
    assert eng.resume_queue() and ("POST", "/queue/resume") in calls

    def bad(method, path, body=None, timeout=60):
        raise EngineError("nope", 400)

    eng._call = bad
    with pytest.raises(EngineError):
        eng.submit_local("c.mp4", title="t", channel="c", platform="twitch", source_url="u", options={})


def test_youtube_chat_prefers_every_message_over_top_chat():
    menu = (
        '"sortFilterSubMenuRenderer":{"subMenuItems":['
        '{"title":"Top chat","selected":true,"continuation":{"reloadContinuationData":'
        '{"continuation":"TOP","clickTrackingParams":"a"}},"accessibility":{"accessibilityData":{"label":"Top"}},'
        '"subtitle":"Some messages"},'
        '{"title":"Live chat","selected":false,"continuation":{"reloadContinuationData":'
        '{"continuation":"ALL","clickTrackingParams":"b"}},"accessibility":{"accessibilityData":{"label":"Live"}},'
        '"subtitle":"All messages are visible"}],"accessibility":{"accessibilityData":{"label":"menu"}}}'
    )
    page = YT_PAGE.replace("</script>", menu + "</script>")
    assert chat.parse_youtube_chat_page(page)[2] == "ALL"


def test_twitch_records_with_streamlink_without_ads(tmp_path, monkeypatch):
    from live_clipper import recorder as rmod

    cmds = []

    class FakePopen:
        def __init__(self, cmd, **kw):
            cmds.append(cmd)
            self.stdout = None

        def poll(self):
            return None

    monkeypatch.setattr(rmod.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(rmod, "streamlink_cmd", lambda: ["streamlink"])
    rmod.Recorder("twitch", "paulinholokobr", tmp_path).start()
    assert cmds[0][0] == "streamlink" and "--stdout" in cmds[0]
    assert "https://www.twitch.tv/paulinholokobr" in cmds[0]
    monkeypatch.setattr(rmod, "streamlink_cmd", lambda: None)
    cmds.clear()
    rmod.Recorder("twitch", "x", tmp_path).start()
    assert "-f" in cmds[0]


def test_jobs_waited_on_survive_a_restart(tmp_path):
    import json as _json

    from live_clipper.config import Channel
    from live_clipper.runner import Clipper

    engine = FakeEngine(tmp_path / "clip.mp4")
    clipper = Clipper(make_cfg(tmp_path), engine=engine)
    clipper._remember(Channel("twitch", "kaicenat"), 4, "local_abc", "20261118-140208", 3.1)
    assert _json.loads((tmp_path / "pending_jobs.json").read_text())[0]["job_id"] == 4

    again = Clipper(make_cfg(tmp_path), engine=engine)  # the next run
    again.telegram = FakeTelegram()
    assert again.resume_pending() == 1
    again.pool.shutdown(wait=True)
    (video, short, full), = again.telegram.sent
    assert "kaicenat" in short
    assert _json.loads((tmp_path / "pending_jobs.json").read_text()) == []


def test_clip_in_the_store_apps_hidden_folder_is_fetched_from_the_app(tmp_path):
    from live_clipper.config import Channel
    from live_clipper.detector import Spike
    from live_clipper.runner import Clipper

    engine = FakeEngine(tmp_path / "AppData" / "not-visible.mp4")  # never created
    clipper = Clipper(make_cfg(tmp_path), engine=engine)
    clipper.telegram = FakeTelegram()
    spike = Spike(at=1000.0, onset=995.0, rate=12, baseline=2, score=6)
    clipper.handle(Channel("twitch", "coringa"), FakeRecorder(), spike, sleep=lambda s: None)
    (video, short, full), = clipper.telegram.sent
    assert video == tmp_path / "outbox" / "clip_3.mp4" and video.read_bytes() == b"mp4"
    assert engine.downloaded == [3]
