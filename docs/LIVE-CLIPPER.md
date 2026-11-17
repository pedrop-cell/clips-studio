# Live clipper

**Clip a stream while it is still live, from the moments its chat explodes.**

The engine itself only works on finished videos (a VOD, a file). The live
clipper is a separate process that sits next to it. For every Twitch or Kick
channel in its list that is live, it:

1. reads the channel's public chat anonymously (no account, no token),
2. keeps the last ten minutes of the stream on disk as short segments,
3. watches for a **chat spike**: the chat rate jumping to several times the
   channel's own usual rate, with reactions (KEKW, kkkk, CLIP, ???) counting
   double,
4. cuts the moment out of the buffer (25 s before the spike's onset, 15 s
   after), with no re-encode,
5. hands the cut to the engine as a local file with the Gaming / Reaction
   layout and one clip, so it comes back framed 9:16 with captions, title
   and hashtags, and
6. delivers it: to a **Telegram chat** for a person to post (free), or
   straight to TikTok, YouTube and the rest through **Upload-Post**.

Chat reacts a few seconds after the moment it reacts to, so the cut is
anchored on the onset of the run-up, not on the second the trigger fired.

## Delivery modes

| `publish.mode` | What happens | Cost | Who approves |
|---|---|---|---|
| `telegram` (default) | The clip and a ready-to-paste caption arrive in a Telegram chat. Post it from your phone to TikTok / Shorts | Free | You, every post |
| `upload_post` | Posted straight to the platforms, inside per-platform daily caps and minimum gaps | TikTok needs a paid Upload-Post plan | Nobody |

Why not the platforms' own APIs for free: an unaudited TikTok app can only
post privately, and YouTube permanently locks videos uploaded from an
unaudited Google Cloud project to private (see
[Publish to YouTube](../README.md#publish-to-youtube)). Both audits are free
but take weeks. Until then, posting by hand from Telegram is the free path.

Every post credits the streamer (`publish.credit`). Programs that pay
clippers require it.

## Configure

```bash
cp live_clipper/config.example.yaml live_clipper.yaml
```

Edit `channels` (`platform:login`, the login from the channel URL) and
`publish`. Every option is commented in the example. The ones that matter
most:

| Key | Default | Meaning |
|---|---|---|
| `detection.ratio` | 3.0 | Spike = recent chat rate this many times the baseline. Lower = more clips |
| `detection.cooldown_s` | 90 | At most one clip per channel in this long |
| `pre_s` / `post_s` | 25 / 15 | Seconds kept before / after the spike's onset |
| `max_parallel_jobs` | 2 | Engine jobs at once. On a CPU-only host keep it at 1–2 |

### Telegram (free mode)

1. In Telegram, talk to **@BotFather**, send `/newbot`, and keep the token it
   gives you.
2. Send any message to your new bot, then open
   `https://api.telegram.org/bot<token>/getUpdates` and copy
   `message.chat.id`. That is your chat id. (A group works too: add the bot
   and use the group's id.)
3. Put the token in the `TELEGRAM_BOT_TOKEN` environment variable and the id
   in `publish.telegram.chat_id` or `TELEGRAM_CHAT_ID`. The token is read
   from the environment only, never from the YAML.

## Run

Next to an engine already running on the same machine:

```bash
python main.py serve --port 8765 &
python -m live_clipper --config live_clipper.yaml
python -m live_clipper --config live_clipper.yaml --dry-run   # make clips, send nothing
```

Needs `ffmpeg` and `yt-dlp` on the PATH (both come with the engine's
requirements and Docker image).

### On an always-on server (Docker)

```bash
export TELEGRAM_BOT_TOKEN=123:abc TELEGRAM_CHAT_ID=456
docker compose -f docker-compose.live.yml up -d --build
docker compose -f docker-compose.live.yml exec ollama ollama pull gemma:7b
docker compose -f docker-compose.live.yml logs -f live
```

The engine answers only requests addressed to localhost, so the `live`
service shares the engine's network namespace and the same `/data` volume.

The image is CPU-only. A 40-second cut takes a minute or two to become a
clip on a 4-core CPU; the buffer holds ten minutes, so cuts queue rather
than get lost. A small free VM (for example an Oracle Cloud Always Free
Ampere instance, 4 cores and 24 GB RAM) is enough for a handful of channels.
If the local model is too slow there, point the engine at OpenRouter
(docs/AI-BACKENDS.md).

## Limits

- **Twitch and Kick only.** YouTube live chat needs an API key and quota.
- **Kick is unofficial.** Its chat socket and channel endpoint are the ones
  kick.com's own player uses; if Kick changes them, Kick channels log errors
  and retry while Twitch keeps working.
- **A spike is chat, not picture.** A quiet visual moment nobody types about
  is not clipped. Streams with slow mode or followers-only chat spike less.
- **Clock alignment** is wall-clock on one machine: chat receive time against
  segment start time. Both lag the streamer by the same few seconds, and the
  25-second lead covers the rest.

## Code

`live_clipper/`: `detector.py` (spikes, pure), `chat.py` (readers),
`recorder.py` (buffer and cuts), `engine.py` (local API client),
`poster.py` (caps and post text), `notify.py` (Telegram), `runner.py`
(one worker per channel). Tests: `tests/test_live_clipper.py`.
