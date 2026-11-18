"""python -m live_clipper --config live_clipper.yaml [--check | --dry-run]"""

import argparse
import logging
import sys
from pathlib import Path

from live_clipper import config, envfile, runner


def check(cfg: config.Config) -> int:
    """Say, in Portuguese, whether everything the clipper needs is in place."""
    from live_clipper.engine import Engine
    from live_clipper.notify import NotifyError, Telegram
    from live_clipper.recorder import ffmpeg_bin, is_live

    ok = True
    if Engine(cfg.engine_url).healthy():
        print("[OK]  App Clips Kitty aberto e respondendo")
    else:
        ok = False
        print("[ERRO] App Clips Kitty não responde. Abra o app e deixe aberto.")
    import shutil

    if shutil.which(ffmpeg_bin()):
        print("[OK]  ffmpeg instalado")
    else:
        ok = False
        print("[ERRO] ffmpeg não encontrado")
    if cfg.publish and cfg.mode == "telegram":
        try:
            Telegram(cfg.telegram_token, cfg.telegram_chat_id).send_text("Teste do Clipador: tudo certo.")
            print("[OK]  Telegram: mensagem de teste enviada")
        except (NotifyError, ValueError) as e:
            ok = False
            print(f"[ERRO] Telegram: {e}")
    print("\nStreamers ao vivo agora:")
    for ch in cfg.channels:
        state = "AO VIVO" if is_live(ch.platform, ch.name) else "offline"
        print(f"  {ch.platform:6} {ch.name:20} {state}")
    print("\nTudo pronto." if ok else "\nCorrija os itens com ERRO e rode de novo.")
    return 0 if ok else 1


def main() -> None:
    ap = argparse.ArgumentParser(prog="live_clipper", description="Clip live streams from chat spikes.")
    ap.add_argument("--config", default="live_clipper.yaml")
    ap.add_argument("--env", default="clipador.env", help="KEY=VALUE file with the Telegram token")
    ap.add_argument("--check", action="store_true", help="check the setup and exit")
    ap.add_argument("--dry-run", action="store_true", help="make clips but never send them")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    envfile.load(Path(args.env))
    envfile.load(Path(".env"))
    cfg = config.load(args.config)
    if args.dry_run:
        cfg.publish = False
    if args.check:
        sys.exit(check(cfg))
    runner.run(cfg)


if __name__ == "__main__":
    main()
