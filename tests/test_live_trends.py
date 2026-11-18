"""Viral GTA 6 search: ranking, dedupe, the Telegram message. No network."""


from live_clipper import config as lc_config
from live_clipper.trends import (
    Seen,
    TrendsConfig,
    Video,
    collect,
    digest,
    idea_for,
    run_once,
    search_url,
)

NOW = 1_800_000_000.0
H = 3600

def entry(vid, views, title="GTA 6 video", channel="Canal", duration=40):
    return {"id": vid, "title": title, "channel": channel, "view_count": views,
            "duration": duration, "url": f"https://www.youtube.com/shorts/{vid}"}

def fake_search(results):
    calls = []

    def search(query, period, limit):
        calls.append((query, period, limit))
        return results.get(query, [])
    search.calls = calls
    return search

def fake_details(ages_h, likes=None):
    def details(url):
        vid = url.rsplit("/", 1)[-1]
        if vid not in ages_h:
            raise RuntimeError("blocked")
        return {"timestamp": NOW - ages_h[vid] * H, "like_count": (likes or {}).get(vid)}
    return details

def test_ranks_by_views_per_hour_not_raw_views():
    cfg = TrendsConfig(queries=["a"], top=2, min_views=1000)
    search = fake_search({"a": [entry("old", 2_000_000), entry("new", 300_000), entry("mid", 500_000)]})
    # old: 2M in 6 days = ~14k/h; new: 300k in 3h = 100k/h; mid: 500k in 2 days ~10k/h
    out = collect(cfg, set(), NOW, search=search, details=fake_details({"old": 144, "new": 3, "mid": 48}))
    assert [v.id for v in out] == ["new", "old"]

def test_skips_seen_small_live_and_duplicate_results():
    cfg = TrendsConfig(queries=["a", "b"], top=5, min_views=1000)
    live = {**entry("live", 90_000), "live_status": "is_live"}
    search = fake_search({"a": [entry("x", 50_000), entry("seen", 80_000), entry("tiny", 10), live],
                          "b": [entry("x", 50_000)]})
    out = collect(cfg, {"seen"}, NOW, search=search, details=fake_details({"x": 5}))
    assert [v.id for v in out] == ["x"]

def test_one_failing_query_or_detail_does_not_stop_the_rest():
    cfg = TrendsConfig(queries=["bad", "good"], top=3, min_views=0)

    def search(q, period, limit):
        if q == "bad":
            raise RuntimeError("HTTP 429")
        return [entry("v", 5000)]
    out = collect(cfg, set(), NOW, search=search, details=fake_details({}))
    assert [v.id for v in out] == ["v"]
    assert out[0].timestamp is None  # unknown age counts as two days

def test_too_old_videos_are_dropped():
    cfg = TrendsConfig(queries=["a"], period="hoje", min_views=0)
    search = fake_search({"a": [entry("y", 900_000), entry("t", 9000)]})
    out = collect(cfg, set(), NOW, search=search, details=fake_details({"y": 30, "t": 2}))
    assert [v.id for v in out] == ["t"]

def test_search_url_sorts_by_views_this_week():
    url = search_url("GTA 6 curiosidades", "semana")
    assert "search_query=GTA+6+curiosidades" in url and url.endswith("sp=CAMSAggD")

def test_ideas_follow_the_title():
    assert "Testei a curiosidade" in idea_for("10 CURIOSIDADES de GTA 6 que você não viu")
    assert "bug" in idea_for("GTA 6 glitch de dinheiro infinito").lower()
    assert "comparativo" in idea_for("GTA 6 vs GTA 5 física")
    assert idea_for("qualquer coisa")

def test_digest_has_links_ideas_tiktok_and_the_warning():
    v = Video(id="a", title="GTA 6 easter egg insano", channel="Fulano",
              url="https://www.youtube.com/shorts/a", views=1_250_000, duration=30,
              timestamp=NOW - 5 * H)
    text = digest([v], NOW)
    assert "Short de Fulano" in text and "1,2 mi views" in text and "há 5h" in text
    assert "https://www.youtube.com/shorts/a" in text
    assert "💡" in text and "tiktok.com/tag/gta6" in text and "strike" in text
    assert len(text) < 4000

class FakeTelegram:
    def __init__(self):
        self.sent = []

    def send_text(self, text, preview=True):
        self.sent.append((text, preview))

def test_run_once_sends_once_and_remembers(tmp_path):
    cfg = TrendsConfig(queries=["a"], min_views=0)
    tg, seen = FakeTelegram(), Seen(tmp_path / "seen.json")
    kw = {"search": fake_search({"a": [entry("v", 5000)]}), "details": fake_details({"v": 1})}
    assert run_once(cfg, tg, seen, now=NOW, **kw) == 1
    assert run_once(cfg, tg, Seen(tmp_path / "seen.json"), now=NOW + 60, **kw) == 0
    assert len(tg.sent) == 1 and tg.sent[0][1] is False

def test_seen_forgets_after_two_weeks(tmp_path):
    s = Seen(tmp_path / "s.json")
    s.add(["old"], NOW)
    s.add(["new"], NOW + 15 * 24 * H)
    assert Seen(tmp_path / "s.json").ids() == {"new"}

def test_config_defaults_on_and_reads_overrides(tmp_path):
    assert lc_config.load("live_clipper/config.example.yaml").trends.enabled
    p = tmp_path / "c.yaml"
    p.write_text("channels: [twitch:a]\n", "utf-8")
    assert lc_config.load(p).trends == TrendsConfig()  # an older yaml still gets it
    p.write_text("channels: [twitch:a]\ntrends: {enabled: false, every_min: 1, period: x}\n", "utf-8")
    t = lc_config.load(p).trends
    assert not t.enabled and t.every_min == 15 and t.period == "semana"
