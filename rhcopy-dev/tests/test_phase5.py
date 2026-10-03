"""Этап 5 (необязательный): большая докупка = вход (conviction_add) и выход зеркалом."""
from conftest import add_position, make_bot
from rhcopy.chain import TRANSFER, pad_addr
from test_forced_facts import PRICE, evm_facts, relay_quote_stub

WALLET = "0xaaaa000000000000000000000000000000000001"
TOKEN = "0xcccc000000000000000000000000000000000003"
ZERO = "0x0000000000000000000000000000000000000000"


def test_conviction_add_becomes_entry(tmp_path, monkeypatch):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001")
    db.add_trader(1, WALLET, "alpha")
    bot.set_override(db.user(1), "gates.add_entry_min_usd", "100")
    net = bot.nets["rh"]
    net.chain._sym[TOKEN] = "CONV"
    net.chain._dec[TOKEN] = 18
    monkeypatch.setattr(net.market, "info", lambda token, fresh=False: {"price_usd": PRICE, "mcap_usd": 200000})
    monkeypatch.setattr(bot, "facts", lambda *a, **k: evm_facts("rh"))
    monkeypatch.setattr(bot.relay, "quote", relay_quote_stub(TOKEN, 6))

    # trader adds 200k tokens (~$200) to a bag he already holds -> above the $100 conviction threshold
    bot.on_add_buy(WALLET, TOKEN, int(200000 * 1e18), 1000, "0xtx", net)

    pos = bot.db.positions(1)
    assert len(pos) == 1 and pos[0]["token"] == TOKEN
    rows = db.c.execute("SELECT decision, reason FROM signals WHERE user_id=1 AND decision='bought'").fetchall()
    assert rows and rows[0]["reason"] == "conviction_add"
    assert any("крупная докупка" in t for t in bot.tg.texts("1001"))


def test_small_add_is_not_first_buy_not_entry(tmp_path, monkeypatch):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001", notify_skips=1)
    db.add_trader(1, WALLET, "alpha")
    bot.set_override(db.user(1), "gates.add_entry_min_usd", "100")
    net = bot.nets["rh"]
    net.chain._sym[TOKEN] = "CONV"
    net.chain._dec[TOKEN] = 18
    monkeypatch.setattr(net.market, "info", lambda token, fresh=False: {"price_usd": PRICE, "mcap_usd": 200000})
    # ~$20 add: below threshold -> no entry, just not_first_buy
    bot.on_add_buy(WALLET, TOKEN, int(20000 * 1e18), 1000, "0xtx", net)
    assert not bot.db.positions(1)
    assert any(s["reason"] == "not_first_buy" for s in db.skipped(1))


def test_mirror_exit_sells_traders_fraction(tmp_path, monkeypatch):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001")
    bot.set_override(db.user(1), "exits.exit_style", "mirror")
    net = bot.nets["rh"]
    pid = add_position(db, 1, "rh", TOKEN, "SYM", WALLET, "alpha", decimals=18, price=PRICE, cost=15.0)
    initial = db.position_by_id(pid)["tokens_initial"]

    # the trader sells half of his bag: balance before = 20000, sold = 10000 -> fraction 0.5
    monkeypatch.setattr(net.chain, "erc20_balance", lambda token, owner, block="latest": int(20000 * 1e18))
    monkeypatch.setattr(bot.relay, "quote", relay_quote_stub(TOKEN, 6))
    log = {"address": TOKEN, "topics": [TRANSFER, pad_addr(WALLET), pad_addr(ZERO)],
           "data": hex(int(10000 * 1e18)), "blockNumber": hex(1000)}
    bot.on_outgoing([log], net)
    assert abs(db.position_by_id(pid)["state"]["mirror_frac"] - 0.5) < 1e-6

    monkeypatch.setattr(net.market, "prices", lambda tokens: {TOKEN: PRICE})
    bot._last_prices = 0
    bot.update_positions()
    p = db.position_by_id(pid)
    assert p["status"] == "open"                       # mirror sold only half, not everything
    assert abs(p["tokens_left"] - initial // 2) <= initial * 0.001
    assert (p["realized_usd"] or 0) > 0
    assert not p["state"].get("mirror_frac")           # consumed
    assert any("ПРОДАЛ" in t and "трейдер вышел" in t for t in bot.tg.texts("1001"))
