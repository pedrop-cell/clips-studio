"""python -m live_clipper --config live_clipper.yaml"""

import argparse
import logging

from live_clipper import config, runner


def main() -> None:
    ap = argparse.ArgumentParser(prog="live_clipper", description="Clip live streams from chat spikes.")
    ap.add_argument("--config", default="live_clipper.yaml")
    ap.add_argument("--dry-run", action="store_true", help="make clips but never publish")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load(args.config)
    if args.dry_run:
        cfg.publish = False
    runner.run(cfg)


if __name__ == "__main__":
    main()
