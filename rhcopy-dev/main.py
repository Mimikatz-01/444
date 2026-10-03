#!/usr/bin/env python3
"""rhcopy — copy FOMO traders on Robinhood Chain.

  python main.py run                      # the bot (paper unless live: true)
  python main.py status                   # every user: mode, cash, positions, PnL
  python main.py route <token> [usd]      # dry run: liquidity, quotes, round-trip loss
  python main.py learnpair <0xaddr>        # find a trader's paired Solana wallet
"""
import argparse
import logging
import os
import sys

import yaml

from rhcopy.bot import CopyBot
from rhcopy.db import DB


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("PRIVATE_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "RPC_URL", "RELAY_API_KEY", "HELIUS_API_KEY",
              "JUPITER_API_KEY", "SOLANA_RPC_URL", "SOLANA_WS_URL", "SOLANA_PRIVATE_KEY", "ETH_RPC_URL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def main():
    ap = argparse.ArgumentParser(description="rhcopy")
    ap.add_argument("cmd", nargs="?", default="run", choices=["run", "status", "route", "learnpair"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--db", default="data/bot.db")
    ap.add_argument("--env", default=".env")
    a = ap.parse_args()

    os.makedirs(os.path.dirname(a.db) or ".", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(os.path.join(os.path.dirname(a.db) or ".", "bot.log"))])
    log = logging.getLogger("rhcopy")
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    with open(a.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    bot = CopyBot(cfg, DB(a.db), load_env(a.env), log)

    if a.cmd == "run":
        bot.run()
    elif a.cmd == "status":
        for u in bot.db.users(active_only=False):
            print(bot.status_text(u))
    elif a.cmd == "route":
        if not a.args:
            sys.exit("usage: route <token> [usd]")
        print(bot.route_report(a.args[0], float(a.args[1]) if len(a.args) > 1 else cfg["sizing"]["ticket_usd"]))
    elif a.cmd == "learnpair":
        if not a.args:
            sys.exit("usage: learnpair <0xaddr|label of the owner's trader>")
        t = bot.db.trader(1, a.args[0])
        addr = t["address"] if t else a.args[0].lower()
        print(bot.learn_pair(addr, t["label"] if t else None))


if __name__ == "__main__":
    main()
