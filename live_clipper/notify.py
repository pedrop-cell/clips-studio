"""Free delivery: send each finished clip to a Telegram chat for a person to post.

The free way to get clips onto TikTok and Shorts without an audited API app:
the clip lands on your phone with its caption ready to copy, you open it,
share it to TikTok or YouTube, paste the caption and post. A person approves
every post, which is also what keeps a new account clear of spam flags.

Two messages per clip: the video (Telegram caps a video caption at 1024
characters, so it carries the title and credit), then the full caption on
its own, to copy in one tap.

Bot API limit for uploads is 50 MB; a 40-second Short is well under it.
Stdlib HTTP only.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

from publish.multipart import encode


class NotifyError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str, api: str = "https://api.telegram.org"):
        if not token or not chat_id:
            raise ValueError("Telegram needs a bot token and a chat id")
        self.base = f"{api}/bot{token}"
        self.chat_id = str(chat_id)

    def _post(self, method: str, body: bytes, content_type: str, timeout: float = 120) -> dict:
        req = urllib.request.Request(f"{self.base}/{method}", data=body, method="POST",
                                     headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                res = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise NotifyError(f"telegram {method}: HTTP {e.code} "
                              f"{e.read().decode('utf-8', 'replace')[:300]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise NotifyError(f"telegram {method}: {e}") from e
        if not res.get("ok"):
            raise NotifyError(f"telegram {method}: {res.get('description')}")
        return res

    def send_text(self, text: str) -> None:
        body = json.dumps({"chat_id": self.chat_id, "text": text[:4000]}).encode()
        self._post("sendMessage", body, "application/json")

    def send_clip(self, video: Path, short_caption: str, full_caption: str) -> None:
        body, ctype = encode(
            [("chat_id", self.chat_id), ("caption", short_caption[:1000]),
             ("supports_streaming", "true")],
            [("video", Path(video))],
        )
        self._post("sendVideo", body, ctype, timeout=300)
        self.send_text(full_caption)


def find_chat_id(token: str, api: str = "https://api.telegram.org") -> str | None:
    """The chat id of the latest message sent to the bot, or None."""
    try:
        with urllib.request.urlopen(f"{api}/bot{token}/getUpdates", timeout=30) as r:
            res = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError(f"telegram getUpdates: {e}") from e
    if not res.get("ok"):
        raise NotifyError(f"telegram getUpdates: {res.get('description')}")
    for update in reversed(res.get("result") or []):
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            return str(chat["id"])
    return None
