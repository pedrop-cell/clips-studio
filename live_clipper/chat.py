"""Read a live channel's public chat. No account, no token.

Twitch: IRC over TLS as an anonymous `justinfan` user, which Twitch allows
for reading any public channel's chat.

Kick: the Pusher websocket Kick's own web player uses. The chatroom id comes
from Kick's channel endpoint, behind the same Cloudflare impersonation that
downloading a Kick VOD already depends on (sources/kick.py). Unofficial: if
Kick changes it, the Kick reader logs and retries, and Twitch keeps working.

Each reader runs `run(on_message, stop)` on its own thread and calls
`on_message(ts, text)` for every chat line, reconnecting on its own.
"""

import json
import logging
import random
import socket
import ssl
import threading
import time
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


def reader_for(platform: str, channel: str, **kw):
    if platform == "twitch":
        return TwitchChat(channel)
    if platform == "kick":
        return KickChat(channel, **kw)
    raise ValueError(f"no chat reader for {platform!r}")
