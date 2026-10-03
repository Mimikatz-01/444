"""Этап 3: confluence entry — buy only when >=N independent watched wallets bought in the window."""
import time

from conftest import make_bot
from rhcopy.relay import Quote

WALLET = "0xaaaa000000000000000000000000000000000001"
WALLET2 = "0xbbbb000000000000000000000000000000000002"
TOKEN = "0xcccc000000000000000000000000000000000003"
SHARED_SOL = "DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6"
PRICE = 0.001


def facts():
    return {"chain": "rh", "age_s": 5.0, "own": True, "paid_eth": False, "stock": False, "rec": None,
            "pair": set(), "tx_from": WALLET, "tx_to": "", "dec": 18, "amount": 50000.0, "origin_usd": 50.0,
            "info": {"price_usd": PRICE, "liquidity_usd": 50000.0, "mcap_usd": 200000.0,
                     "pair_created_ms": int((time.time() - 3600) * 1000), "buys_h24": 10, "sells_h24": 5}}


def quote(user, cin, cout, amount_raw, slippage_bps=None, chain_id=None):
    if cout.lower() == TOKEN:
        usd = amount_raw / 1e6
        out = int(usd / PRICE * 1e18)
        return Quote("r", out, out, usd, usd, -1.0, 0.05, 0.0, [])
    out = int(amount_raw / 1e18 * PRICE * 0.99 * 1e6)
    return Quote("r", out, out, 0.0, 0.0, -1.0, 0.05, 0.0, [])


def setup(tmp_path, monkeypatch, cmin=2, shared=False):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001", notify_skips=1)
    db.add_trader(1, WALLET, "alpha")
    db.add_trader(1, WALLET2, "beta")
    if shared:   # one person behind both wallets: shared Solana pair -> one source
        db.set_pair(WALLET, SHARED_SOL)
        db.set_pair(WALLET2, SHARED_SOL)
    bot.set_override(db.user(1), "gates.confluence_min", str(cmin))
    bot.nets["rh"].chain._sym[TOKEN] = "CONF"
    monkeypatch.setattr(bot, "facts", lambda *a, **k: facts())
    monkeypatch.setattr(bot.relay, "quote", quote)
    return bot


def pending_rows(bot):
    return bot.db.c.execute("SELECT decision, source FROM signals WHERE user_id=1 AND decision='pending'").fetchall()


def test_confluence_triggers_on_second_independent_wallet(tmp_path, monkeypatch):
    bot = setup(tmp_path, monkeypatch, cmin=2)
    net = bot.nets["rh"]

    bot.on_signal(WALLET, TOKEN, int(50000 * 1e18), 1, "0xtx1", net)   # first wallet: just buffer
    assert not bot.db.positions(1)
    assert not any("КУПИЛ" in t for t in bot.tg.texts("1001"))
    assert pending_rows(bot) and pending_rows(bot)[0]["source"] == "confluence"

    bot.on_signal(WALLET2, TOKEN, int(50000 * 1e18), 2, "0xtx2", net)  # second independent wallet: enter
    pos = bot.db.positions(1)
    assert len(pos) == 1 and pos[0]["source"] == "confluence"
    assert any("совпадение" in t and "alpha + beta" in t for t in bot.tg.texts("1001"))


def test_same_person_counts_as_one_source(tmp_path, monkeypatch):
    bot = setup(tmp_path, monkeypatch, cmin=2, shared=True)
    net = bot.nets["rh"]
    bot.on_signal(WALLET, TOKEN, int(50000 * 1e18), 1, "0xtx1", net)
    bot.on_signal(WALLET2, TOKEN, int(50000 * 1e18), 2, "0xtx2", net)
    assert not bot.db.positions(1)   # both wallets are the same person -> never two independent sources


def test_window_expiry_reports_no_confluence(tmp_path, monkeypatch):
    bot = setup(tmp_path, monkeypatch, cmin=2)
    net = bot.nets["rh"]
    bot.on_signal(WALLET, TOKEN, int(50000 * 1e18), 1, "0xtx1", net)
    key = (1, "rh", TOKEN)
    assert key in bot.pending
    for e in bot.pending[key]:    # fast-forward past the 10-minute window
        e["ts"] -= 3600
    bot.sweep_confluence()
    assert key not in bot.pending
    assert any(s["reason"] == "no_confluence" for s in bot.db.skipped(1))
    assert any("совпадени" in t.lower() for t in bot.tg.texts("1001"))
