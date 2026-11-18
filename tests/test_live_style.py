import shutil
import subprocess

import pytest

from live_clipper import config
from live_clipper.style import ClipStyle, hook_ass, hook_text, with_hook


def test_hook_text_is_short_uppercase_lines_without_emoji():
    t = hook_text("{title}", "Alanzoka não acreditou no que viu 😱🔥 no GTA 6", "alanzoka")
    lines = t.split("\n")
    assert t == t.upper() and "😱" not in t and "NÃO" in t
    assert len(lines) <= 3 and all(len(line) <= 21 for line in lines)


def test_hook_text_falls_back_to_channel():
    assert hook_text("{title}", "😱😱", "gaules") == "GAULES AO VIVO"


def test_hook_ass_shows_only_first_seconds_at_top():
    ass = hook_ass("OI {X}\nTCHAU", ClipStyle(hook_s=3))
    assert "0:00:00.00,0:00:03.00" in ass and "OI X\\NTCHAU" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style: Hook"))
    assert style.split(",")[18] == "8"  # top-centre


def _cfg(tmp_path, body):
    p = tmp_path / "c.yaml"
    p.write_text("channels: [twitch:gaules]\n" + body, "utf-8")
    return config.load(p)


def test_old_example_values_get_the_new_cut(tmp_path):
    c = _cfg(tmp_path, "pre_s: 25\npost_s: 15\nengine_options:\n  focus: "
             "\"the strongest reaction or hype moment; start right before it happens\"\n")
    assert (c.pre_s, c.post_s) == (config.PRE_S, config.POST_S)
    assert c.engine_options["focus"] == config.FOCUS
    assert c.engine_options["caption_style"]["position"] == "middle"
    assert c.clip_style.hook and not c.clip_style.outro


def test_chosen_values_are_kept(tmp_path):
    c = _cfg(tmp_path, "pre_s: 20\npost_s: 15\nclip_style: {hook: false, hook_s: 2}\n")
    assert (c.pre_s, c.post_s) == (20, 15)
    assert not c.clip_style.hook and c.clip_style.hook_s == 2


def test_with_hook_off_or_broken_returns_the_original(tmp_path, monkeypatch):
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"not a video")
    assert with_hook(v, "x", "y", ClipStyle(hook=False), tmp_path / "out") == v
    monkeypatch.setattr("live_clipper.style.ffmpeg_bin", lambda: "definitely-not-ffmpeg")
    assert with_hook(v, "x", "y", ClipStyle(), tmp_path / "out") == v


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_with_hook_burns_a_new_file(tmp_path):
    src = tmp_path / "app" / "clip.mp4"
    src.parent.mkdir()
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "color=c=blue:s=1080x1920:d=4", "-f", "lavfi", "-i", "sine=d=4",
                        "-shortest", "-c:v", "libx264", "-c:a", "aac", str(src)])
    assert r.returncode == 0
    out = with_hook(src, "Gaules gritou demais", "gaules", ClipStyle(), tmp_path / "outbox")
    if out == src:
        pytest.skip("this ffmpeg has no libass")
    assert out.parent == tmp_path / "outbox" and out.stat().st_size > 0
