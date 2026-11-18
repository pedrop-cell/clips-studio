"""First-run setup, in Portuguese: Telegram bot and a config of your own.

python -m live_clipper.setup [--folder DIR]

Asks for the bot token, waits for you to message the bot so it can read the
chat id by itself, sends a test message, and writes clipador.env and
live_clipper.yaml into the folder. Run again to change the bot.
"""

import argparse
import os
import re
import time
from pathlib import Path

from live_clipper import envfile
from live_clipper.notify import NotifyError, Telegram, find_chat_id

EXAMPLE = Path(__file__).with_name("config.example.yaml")
TOKEN_RE = re.compile(r"^\d{6,}:[\w-]{30,}$")


def write_config(folder: Path) -> Path:
    """live_clipper.yaml from the example, with work_dir inside `folder`."""
    out = folder / "live_clipper.yaml"
    if out.exists():
        return out
    text = EXAMPLE.read_text("utf-8")
    work = (folder / "dados").resolve().as_posix()
    text = re.sub(r"^work_dir: .*$", f"work_dir: {work}            # cortes, buffer e registro",
                  text, count=1, flags=re.MULTILINE)
    out.write_text(text, "utf-8")
    return out


def clean_token(raw: str) -> str:
    """The token out of whatever was pasted: a bare token, KEY=token, quotes."""
    raw = (raw or "").strip().strip("\ufeff")
    if "=" in raw:
        raw = raw.split("=", 1)[1]
    return raw.strip().strip('"').strip("'").strip()


def token_from_file(folder: Path) -> str:
    """token.txt in the folder: the way in when pasting into the window fails."""
    try:
        return clean_token((folder / "token.txt").read_text("utf-8-sig"))
    except OSError:
        return ""


def ask_token(ask=input) -> str:
    # Visible on purpose: a hidden prompt (getpass) ignores Ctrl+V on Windows
    # consoles, and this is your own screen.
    print("Dica: se colar não funcionar, clique com o botão DIREITO do mouse na janela.")
    print("Ou feche, salve o token num arquivo token.txt nesta pasta e abra de novo.")
    while True:
        token = clean_token(ask("Cole o token do bot do Telegram e aperte Enter: "))
        if TOKEN_RE.match(token):
            return token
        print("Esse texto não parece um token. Ele tem o formato 123456789:ABC... Tente de novo.")


def wait_chat_id(token: str, timeout_s: float = 300, sleep=time.sleep, find=find_chat_id) -> str | None:
    print("\nAgora abra o seu bot no Telegram e mande uma mensagem qualquer, tipo 'oi'.")
    print("Estou esperando a mensagem chegar...")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        chat = find(token)
        if chat:
            return chat
        sleep(3)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="live_clipper.setup")
    ap.add_argument("--folder", default=".")
    ap.add_argument("--if-needed", action="store_true", help="do nothing if already set up")
    args = ap.parse_args()
    folder = Path(args.folder).resolve()
    env_path = folder / "clipador.env"
    envfile.load(env_path)
    envfile.load(folder / ".env")
    token = clean_token(os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if args.if_needed and TOKEN_RE.match(token) and chat:
        envfile.save(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
        write_config(folder)
        return 0

    print("=== Configuração do Clipador ===\n")
    if not TOKEN_RE.match(token):
        token = token_from_file(folder)
    if TOKEN_RE.match(token):
        print("Token do Telegram encontrado.")
    else:
        token = ask_token()
    try:
        if not chat:
            chat = wait_chat_id(token)
    except NotifyError as e:
        print(f"\nO Telegram recusou o token: {e}\nConfira o token no @BotFather e rode de novo.")
        return 1
    if not chat:
        print("\nNão chegou nenhuma mensagem em 5 minutos. Mande 'oi' para o bot e rode de novo.")
        return 1
    try:
        Telegram(token, chat).send_text("Clipador conectado! Os clipes vão chegar aqui.")
    except NotifyError as e:
        print(f"\nNão consegui mandar a mensagem de teste: {e}")
        return 1
    envfile.save(env_path, {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat})
    (folder / "token.txt").unlink(missing_ok=True)  # now kept in clipador.env
    cfg = write_config(folder)
    print("\nPronto! Você deve ter recebido 'Clipador conectado!' no Telegram.")
    print(f"Lista de streamers: {cfg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
