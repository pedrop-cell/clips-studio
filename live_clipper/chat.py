"""Read a live channel's public chat. No account, no token.

Twitch: IRC over TLS as an anonymous `justinfan` user, which Twitch allows
for reading any public channel's chat.

Kick: the Pusher websocket Kick's own web player uses. The chatroom id comes
from Kick's channel endpoint, behind the same Cloudflare impersonation that
downloading a Kick VOD already depends on (sources/kick.py). Unofficial: if
Kick changes it, the Kick reader logs and retries, and Twitch keeps working.

YouTube: the same polling YouTube's own popout chat page does (the
`live_chat/get_live_chat` call of its web API, with the public key the page
embeds). Unofficial too; it logs and retries the same way.

Each reader runs `run(on_message, stop)` on its own thread and calls
`on_message(ts, text)` for every chat line, reconnecting on its own.
"""

import json
import logging
import random
import re
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable

log = logging.getLogger("live_clipper.chat")

OnMessage = Callable[[float, str], None]

TWITCH_HOST = "irc.chat.twitch.tv"
TWITCH_PORT = 6697

# Kick's public Pusher app key and cluster, as served to every browser by
# kick.com. Overridable in the config in case Kick rotates it.
KICK_PUSHER_URL = (
    "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679"
    "?protocol=7&client=js&version=8.4.0&flash=false"
)


def parse_privmsg(line: str) -> str | None:
    """The message text of an IRC PRIVMSG line, or None for anything else."""
    if line.startswith("@"):
        _, _, line = line.partition(" ")
    if " PRIVMSG " not in line:
        return None
    _, _, rest = line.partition(" PRIVMSG ")
    _, sep, text = rest.partition(" :")
    return text if sep else None


class TwitchChat:
    def __init__(self, channel: str, connect: Callable[[], socket.socket] | None = None):
        self.channel = channel.lower().lstrip("#")
        self._connect = connect or self._tls_socket

    @staticmethod
    def _tls_socket() -> socket.socket:
        raw = socket.create_connection((TWITCH_HOST, TWITCH_PORT), timeout=30)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=TWITCH_HOST)

    def run(self, on_message: OnMessage, stop: threading.Event) -> None:
        backoff = 2.0
        while not stop.is_set():
            try:
                self._session(on_message, stop)
                backoff = 2.0
            except (OSError, ssl.SSLError) as e:
                log.warning("twitch chat %s: %s; reconnecting in %.0fs", self.channel, e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60.0)

    def _session(self, on_message: OnMessage, stop: threading.Event) -> None:
        sock = self._connect()
        sock.settimeout(5)
        try:
            nick = f"justinfan{random.randint(10000, 99999)}"
            sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
            log.info("twitch chat: joined #%s", self.channel)
            buf = b""
            last_rx = time.monotonic()
            while not stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    if time.monotonic() - last_rx > 360:
                        raise OSError("no data for 6 minutes") from None
                    continue
                if not chunk:
                    raise OSError("connection closed")
                last_rx = time.monotonic()
                buf += chunk
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        sock.sendall(("PONG" + line[4:] + "\r\n").encode())
                        continue
                    if " RECONNECT" in line:
                        raise OSError("server asked to reconnect")
                    text = parse_privmsg(line)
                    if text is not None:
                        on_message(time.time(), text)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def impersonation() -> dict:
    """yt-dlp options to look like Chrome to Cloudflare (needs curl_cffi).

    The same trick sources/kick.py uses, kept here so the clipper imports
    nothing from the engine's pipeline and runs on a bare Python install.
    """
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return {"impersonate": ImpersonateTarget.from_str("chrome")}
    except Exception:
        return {}


def kick_chatroom_id(slug: str) -> int:
    """Kick's chatroom id for a channel slug (one Cloudflare-impersonated GET)."""
    import yt_dlp
    from yt_dlp.networking import Request

    url = f"https://kick.com/api/v2/channels/{slug}"
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **impersonation()}) as ydl:
        with ydl.urlopen(Request(url, headers={"Accept": "application/json"},
                                 extensions=impersonation())) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    return int(data["chatroom"]["id"])


def parse_pusher(frame: str) -> str | None:
    """The chat text of a Kick Pusher frame, or None for anything else."""
    try:
        msg = json.loads(frame)
    except ValueError:
        return None
    if not str(msg.get("event", "")).endswith("ChatMessageEvent"):
        return None
    data = msg.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    content = data.get("content")
    return content if isinstance(content, str) else None


class KickChat:
    def __init__(self, channel: str, pusher_url: str = KICK_PUSHER_URL,
                 chatroom_id: Callable[[str], int] = kick_chatroom_id):
        self.channel = channel.lower()
        self.pusher_url = pusher_url
        self._chatroom_id = chatroom_id

    def run(self, on_message: OnMessage, stop: threading.Event) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                self._session(on_message, stop)
                backoff = 5.0
            except Exception as e:  # websockets raises many types; all mean reconnect
                log.warning("kick chat %s: %s; reconnecting in %.0fs", self.channel, e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)

    def _session(self, on_message: OnMessage, stop: threading.Event) -> None:
        from websockets.sync.client import connect

        room = self._chatroom_id(self.channel)
        with connect(self.pusher_url, open_timeout=20) as ws:
            ws.send(json.dumps({"event": "pusher:subscribe",
                                "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
            log.info("kick chat: joined %s (room %s)", self.channel, room)
            last_ping = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_ping > 60:
                    ws.send(json.dumps({"event": "pusher:ping", "data": {}}))
                    last_ping = time.monotonic()
                try:
                    frame = ws.recv(timeout=5)
                except TimeoutError:
                    continue
                text = parse_pusher(frame if isinstance(frame, str) else frame.decode("utf-8", "replace"))
                if text is not None:
                    on_message(time.time(), text)


YT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_CONT_KINDS = ("invalidationContinuationData", "timedContinuationData",
               "reloadContinuationData", "liveChatReplayContinuationData")


def _http(url: str, body: dict | None = None, timeout: float = 20) -> str:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": YT_UA, "Accept-Language": "pt-BR,pt;q=0.9",
        **({"Content-Type": "application/json"} if data else {}),
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_youtube_chat_page(html: str) -> tuple[str, str, str]:
    """(api key, client version, first continuation) from the popout chat page."""
    key = re.search(r'"INNERTUBE_API_KEY"\s*:\s*"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION"\s*:\s*"([^"]+)"', html)
    cont = re.search(r'"(?:invalidation|timed|reload)ContinuationData"\s*:\s*\{(?:[^{}]|\{[^{}]*\})*?'
                     r'"continuation"\s*:\s*"([^"]+)"', html)
    if not (key and cont):
        raise OSError("youtube chat page without a key or continuation (chat off or not live?)")
    return key.group(1), (ver.group(1) if ver else "2.20260101.00.00"), cont.group(1)


def _runs_text(message: dict | None) -> str:
    out = []
    for run in (message or {}).get("runs") or []:
        if "text" in run:
            out.append(run["text"])
        elif "emoji" in run:
            emoji = run["emoji"]
            shortcuts = emoji.get("shortcuts") or []
            out.append(shortcuts[0] if shortcuts else emoji.get("emojiId", ""))
    return "".join(out)


def parse_youtube_chat(data: dict) -> tuple[list[tuple[float | None, str]], str | None, float]:
    """Messages, next continuation and wait (s) from one get_live_chat answer.

    Each message is (timestamp or None, text). A None continuation means the
    chat ended.
    """
    lcc = (data.get("continuationContents") or {}).get("liveChatContinuation") or {}
    msgs: list[tuple[float | None, str]] = []
    for action in lcc.get("actions") or []:
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        for kind in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer"):
            r = item.get(kind)
            if not r:
                continue
            text = _runs_text(r.get("message"))
            if not text:
                continue
            try:
                ts = int(r["timestampUsec"]) / 1e6
            except (KeyError, ValueError, TypeError):
                ts = None
            msgs.append((ts, text))
    cont, wait = None, 5.0
    for c in lcc.get("continuations") or []:
        for kind in _CONT_KINDS:
            if kind in c and c[kind].get("continuation"):
                cont = c[kind]["continuation"]
                if c[kind].get("timeoutMs"):
                    wait = int(c[kind]["timeoutMs"]) / 1000
                break
        if cont:
            break
    return msgs, cont, min(max(wait, 1.0), 10.0)


class YouTubeChat:
    def __init__(self, channel: str, http: Callable[..., str] = _http,
                 video_id: Callable[[str], str | None] | None = None):
        self.channel = channel
        self._http = http
        if video_id is None:
            from live_clipper.recorder import youtube_live_video_id as video_id
        self._video_id = video_id

    def run(self, on_message: OnMessage, stop: threading.Event) -> None:
        backoff = 5.0
        while not stop.is_set():
            try:
                self._session(on_message, stop)
                backoff = 5.0
            except Exception as e:  # HTTP, JSON and page changes all mean retry
                log.warning("youtube chat %s: %s; reconnecting in %.0fs", self.channel, e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 120.0)

    def _session(self, on_message: OnMessage, stop: threading.Event) -> None:
        vid = self._video_id(self.channel)
        if not vid:
            raise OSError("not live")
        html = self._http(f"https://www.youtube.com/live_chat?is_popout=1&v={vid}")
        key, version, cont = parse_youtube_chat_page(html)
        log.info("youtube chat: joined %s (video %s)", self.channel, vid)
        url = f"https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key={key}&prettyPrint=false"
        ctx = {"client": {"clientName": "WEB", "clientVersion": version, "hl": "pt", "gl": "BR"}}
        while not stop.is_set():
            data = json.loads(self._http(url, {"context": ctx, "continuation": cont}))
            msgs, nxt, wait = parse_youtube_chat(data)
            now = time.time()
            for ts, text in msgs:
                # YouTube hands chat over in batches; keep each line's own time,
                # within reason, so a burst stays a burst.
                on_message(now if ts is None else min(now, max(ts, now - 30)), text)
            if nxt is None:
                raise OSError("chat ended")
            cont = nxt
            stop.wait(wait)


def reader_for(platform: str, channel: str, **kw):
    if platform == "twitch":
        return TwitchChat(channel)
    if platform == "kick":
        return KickChat(channel, **kw)
    if platform == "youtube":
        return YouTubeChat(channel, **kw)
    raise ValueError(f"no chat reader for {platform!r}")
