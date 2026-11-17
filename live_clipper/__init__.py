"""Live clipper: clip a stream while it is still live, from its chat.

A sidecar to the engine. It watches a list of Twitch and Kick channels, and
for each one that is live it:

1. reads the public chat (no account needed) and scores every second of it,
2. records the stream into a short rolling buffer of segments on disk,
3. on a chat spike, cuts the buffer around the moment into an mp4,
4. hands that file to the engine (`POST /videos/local`, Gaming layout, one
   clip), waits for the clip, and
5. publishes it through Upload-Post, inside per-platform daily caps.

- detector.py  chat rate and hype keywords -> spikes (pure, tested)
- chat.py      Twitch IRC (anonymous) and Kick (Pusher websocket) readers
- recorder.py  yt-dlp | ffmpeg rolling segment buffer, and cutting from it
- engine.py    the engine's local API: submit, wait, read clips, publish
- poster.py    daily caps and spacing per platform, and the post text
- runner.py    one worker per channel tying the above together
- config.py    the YAML config

Everything that touches the network or a process takes it as an argument or
sits behind a small class, so the tests never do. See docs/LIVE-CLIPPER.md.
"""
