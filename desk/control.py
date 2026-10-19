"""Switches from the command line: control.bat <command>

  control.bat pause             no new trades (open positions still managed)
  control.bat resume            allow new trades again
  control.bat resume --confirm  lift the kill switch
  control.bat kill              stop trading + AI; stops still protect positions
  control.bat kill --close-all  ...and sell every open position
  control.bat close-all         sell every open position (bot keeps running)

The running bot picks the change up within a minute.
"""

from __future__ import annotations

import argparse
import sys

from desk.clock import Clock
from desk.config import ConfigError, load_config
from desk.controls import ControlPanel
from desk.db import Database


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Pause / resume / kill the trading bot")
    p.add_argument("command", choices=["pause", "resume", "kill", "close-all", "show"])
    p.add_argument("--confirm", action="store_true", help="with resume: also lift the kill switch")
    p.add_argument("--close-all", action="store_true", help="with kill: also sell every open position")
    p.add_argument("--config", help="path to a config.yaml")
    args = p.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"CONFIG ERROR\n{exc}")
        return 2
    clock = Clock(config.tz)
    db = Database(config.path("database"), clock)
    db.migrate()
    panel = ControlPanel(db, clock)
    try:
        if args.command == "pause":
            print(panel.pause(by="control.bat"))
        elif args.command == "resume":
            ok, msg = panel.resume(by="control.bat", confirm=args.confirm)
            print(msg)
            if not ok:
                return 1
        elif args.command == "kill":
            print(panel.kill(by="control.bat", close_all=args.close_all))
        elif args.command == "close-all":
            print(panel.request_close_all(by="control.bat"))
        print(f"Switches now: {panel.state().describe()}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
