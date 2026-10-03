"""Shared test helpers: a fake Telegram transport, a bot factory and an offline network guard.

Offline tests never touch the network: requests.Session.send is replaced and every client in the bot
already turns a connection error into "no data". Tests marked `network` use real quotes (paper mode)
and are skipped when the internet is unreachable or RHCOPY_OFFLINE=1."""
import json
import logging
import os
import queue
import socket
import sqlite3
import sys
import time
from pathlib import Path

import pytest
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rhcopy import relay as relay_mod  # noqa: E402
from rhcopy.bot import CopyBot  # noqa: E402
from rhcopy.db import DB  # noqa: E402
from rhcopy.ui import UI  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
LOG = logging.getLogger("rhcopy-test")

# public trader wallets (on-chain addresses, no secrets)
EVM1 = "0xddd462bb053b57d5d73c9615e11a7284cfee9233"
SOL1 = "DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6"
EVM2 = "0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e"
SOL2 = "4XPvoZQrTnGZuTiYqMm2z6JbeHMtsZQfJyYMJCwgMKot"
SOL_ONLY = "DjK6ADK2pc8VCbTWuFuPzDBctDTy7aJbz4zBLkFVjJFY"


class FakeTG:
    """Records what the bot would send to Telegram."""

    def __init__(self):
        self.inbox = queue.Queue()
        self.sent, self.edits, self.answers = [], [], []

    def send(self, chat, text, kb=None, reply_kb=None, extra=None, delete_after=None):
        self.sent.append({"chat": chat, "text": text, "kb": kb, "reply_kb": reply_kb, "extra": extra,
                          "delete_after": delete_after})

    def edit(self, chat, msg_id, text, kb=None):
        self.edits.append({"chat": chat, "msg_id": msg_id, "text": text, "kb": kb})

    def answer(self, cb_id, text=None):
        self.answers.append({"cb_id": cb_id, "text": text})

    def start(self):
        pass

    def texts(self, chat=None):
        return [m["text"] for m in self.sent if chat is None or str(m["chat"]) == str(chat)]


def load_cfg(name="config.example.yaml"):
    with open(ROOT / name, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg.setdefault("telegram", {})["enabled"] = False
    return cfg


def make_bot(db_path, cfg=None, env=None, tg=True):
    bot = CopyBot(cfg or load_cfg(), DB(str(db_path)), env or {}, LOG)
    if tg:
        bot.tg = FakeTG()
        bot.ui = UI(bot)
    return bot


def load_sql(db_path, name):
    con = sqlite3.connect(str(db_path))
    con.executescript((FIX / name).read_text(encoding="utf-8"))
    con.commit()
    con.close()


def add_position(db, uid, chain, token, symbol, wallet, label, status="open", cost=15.0, realized=0.0,
                 decimals=6, price=0.001, last=None, opened=None, **extra):
    raw = int(cost / price * 10 ** decimals)
    now = time.time()
    fields = dict(user_id=uid, chain=chain, token=token, symbol=symbol, decimals=decimals, wallet=wallet, label=label,
                  opened=opened or now - 600, status=status, paper=1, cost_usd=cost, tokens_initial=str(raw),
                  tokens_left=str(0 if status != "open" else raw), entry_price=price, gas_usd=0.02,
                  last_price=last or price, peak_price=max(price, last or price), realized_usd=realized,
                  state=json.dumps({"origin_tx": "0xabc"}))
    if status != "open":
        fields["closed"] = now - 60
    fields.update(extra)
    return db.open_position(**fields)


def seed(bot):
    """Two users with traders, open and closed positions, skipped signals and a muted reason."""
    db = bot.db
    db.update_user(1, chat_id="1001", name="owner")
    uid2 = db.add_user("1002", "friend", 500.0)
    db.add_trader(1, EVM1, "alpha")
    db.set_pair(EVM1, SOL1)
    db.add_trader(1, SOL_ONLY, "solguy")
    db.set_pair(SOL_ONLY, SOL_ONLY)
    db.add_trader(uid2, EVM1, "alpha", 25)
    db.add_trader(uid2, EVM2, "beta")
    db.set_pair(EVM2, SOL2)
    add_position(db, 1, "sol", "5GDX5fJTQns4arM1J94wjxdA8KXFsV5qW46MLFpHpump", "OMNI", SOL_ONLY, "solguy")
    add_position(db, 1, "rh", "0x9e795209be691654fb908ab86b3a651551939e16", "TALON", EVM1, "alpha",
                 status="closed", realized=19.0, decimals=18)
    add_position(db, uid2, "eth", "0x6982508145454ce325ddbe47a25d4ec3d2311933", "PEPE", EVM1, "alpha", decimals=18)
    add_position(db, uid2, "sol", "A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS", "ZEC", EVM2, "beta",
                 status="writeoff", realized=0.0, decimals=8)
    for uid in (1, uid2):
        db.signal(uid, EVM1, "alpha", "0x02fa7c6b92a501aa6389814479d355413f9784f8", "SICAT", "0x1", 1, "skipped",
                  "impact", {"chain": "rh", "impact": -7.3, "price": 0.0001, "mcap": 100000})
        db.signal(uid, EVM1, "alpha", "fCUBpdeRn76xfRaa4UPHDauGeRG3EvMB3MjuRgdpump", "DARKPOOL", "sig", 1, "skipped",
                  "chase", {"chain": "sol", "chase": 31.0, "price": 0.00007, "mcap": 70000})
    db.update_user(uid2, muted=json.dumps(["chase"]))
    return uid2


_ONLINE = None


def online():
    global _ONLINE
    if _ONLINE is None:
        if os.environ.get("RHCOPY_OFFLINE"):
            _ONLINE = False
        else:
            try:
                socket.create_connection(("api.relay.link", 443), timeout=5).close()
                _ONLINE = True
            except OSError:
                _ONLINE = False
    return _ONLINE


@pytest.fixture(autouse=True)
def _network_guard(request, monkeypatch):
    if request.node.get_closest_marker("network"):
        if not online():
            pytest.skip("no internet (or RHCOPY_OFFLINE=1)")
        return

    def blocked(self, req, **kw):
        raise requests.ConnectionError(f"network disabled in offline tests: {req.url}")

    monkeypatch.setattr(requests.Session, "send", blocked)
    monkeypatch.setattr(relay_mod.Relay, "_load_solvers", lambda self: None)


@pytest.fixture
def bot(tmp_path):
    return make_bot(tmp_path / "bot.db")
