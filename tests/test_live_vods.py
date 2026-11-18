"""Today's finished lives sent whole to Clips Kitty. Fakes only, no network."""

from live_clipper import config as lc_config
from live_clipper.config import Channel, Config
from live_clipper.vods import VodConfig, listing_url, recent_vods, run_once, seconds_until

NOW = 1_763_500_000.0


def test_listing_urls():
    assert listing_url("twitch", "mount") == "https://www.twitch.tv/mount/videos?filter=archives&sort=time"
    assert listing_url("youtube", "@mountzera") == "https://www.youtube.com/@mountzera/streams"
    assert listing_url("youtube", "UCabcdefghijklmnopqrstUV").endswith("/channel/UCabcdefghijklmnopqrstUV/streams")
    assert listing_url("youtube", "watch:abcdefghijk") is None
    assert listing_url("kick", "n3on") is None


def test_recent_vods_keeps_finished_lives_from_today():
    def extract(url, flat, size):
        if flat:
            return {"entries": [{"url": "v1"}, {"url": "v2"}, {"url": "v3"}]}
        return {
            "v1": {"live_status": "is_live", "timestamp": NOW - 600},
            "v2": {"live_status": "was_live", "timestamp": NOW - 3 * 3600, "duration": 7200,
                   "webpage_url": "https://www.twitch.tv/videos/2", "title": "live"},
            "v3": {"live_status": "was_live", "timestamp": NOW - 30 * 3600},
        }[url]

    got = recent_vods("twitch", "mount", NOW - 24 * 3600, 3, extract=extract)
    assert [v["url"] for v in got] == ["https://www.twitch.tv/videos/2"]


def test_seconds_until_next_local_time():
    # NOW is 18:06:40 in Brasilia (UTC-3)
    assert round(seconds_until("19:00", -3, now=NOW)) == 3200
    assert round(seconds_until("06:00", -3, now=NOW)) == 11 * 3600 + 53 * 60 + 20


class FakeEngine:
    def __init__(self):
        self.urls = []

    def resume_queue(self):
        return True

    def submit_url(self, url, options, on_full=None):
        self.urls.append((url, options))
        return 11

    def wait_job(self, job_id, **kw):
        return {"status": "done", "video_id": "twitch_2"}

    def clips(self, video_id):
        return [{"id": i, "score": i, "path": f"c{i}.mp4"} for i in range(8)]


class FakeClipper:
    def __init__(self):
        self.engine = FakeEngine()
        self.telegram = object()
        self.stop_event = None
        self.sent = []

    def _deliver(self, ch, clip, note, name, url):
        self.sent.append((ch.key, clip["id"], note))


def test_run_once_sends_each_live_once_with_its_best_clips(tmp_path):
    cfg = Config(channels=[Channel("twitch", "mount"), Channel("kick", "n3on")], work_dir=tmp_path,
                 engine_options=dict(lc_config.DEFAULT_ENGINE_OPTIONS), vods=VodConfig(max_clips=3))
    seen_platforms = []

    def find(platform, name, since, per_channel):
        seen_platforms.append(platform)
        return [{"url": "https://www.twitch.tv/videos/2", "title": "", "started": NOW - 3600, "duration": 7200}]

    clipper = FakeClipper()
    assert run_once(cfg, clipper, now=NOW, find=find) == 1
    assert seen_platforms == ["twitch"]
    (url, options), = clipper.engine.urls
    assert options["max_clips"] == 3
    assert [c[1] for c in clipper.sent] == [7, 6, 5]
    assert clipper.sent[0][2].startswith("live de ")
    assert run_once(cfg, clipper, now=NOW, find=find) == 0  # already done
