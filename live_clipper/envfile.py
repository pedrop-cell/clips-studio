"""clipador.env: KEY=VALUE lines read into the environment at start.

Where the Windows setup keeps the Telegram token and chat id, so they never
go into the YAML (which people copy and share) or a chat.
"""

import os
from pathlib import Path


def load(path: Path) -> None:
    try:
        lines = Path(path).read_text("utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


def save(path: Path, values: dict[str, str]) -> None:
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")
