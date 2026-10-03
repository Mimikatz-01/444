"""Этап 1: signal-source model — not_first_buy reports and the 'По источникам' stats."""
from conftest import add_position, make_bot

WALLET = "0xaaaa000000000000000000000000000000000001"
WALLET2 = "0xbbbb000000000000000000000000000000000002"
TOKEN1 = "0xcccc000000000000000000000000000000000003"
TOKEN2 = "5GDX5fJTQns4arM1J94wjxdA8KXFsV5qW46MLFpHpump"


def test_not_first_buy_reports_and_respects_holding(tmp_path):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001", notify_skips=1)
    db.add_trader(1, WALLET, "alpha")

    bot.report_not_first_buy(db.followers(WALLET), TOKEN1, "AAA", 42.0, 100, "0xtx", "rh",
                             {"price_usd": 0.001, "mcap_usd": 200000})
    assert any(s["reason"] == "not_first_buy" for s in db.skipped(1))
    assert not db.positions(1)  # an add never buys
    sent = [m for m in bot.tg.sent if "докупил" in m["text"]]
    assert sent and any(btn.get("callback_data") == "mute:not_first_buy" for row in sent[0]["kb"] for btn in row)

    # once we already hold the coin, an add is ordinary — say nothing
    before = len(bot.tg.sent)
    add_position(db, 1, "rh", TOKEN1, "AAA", WALLET, "alpha")
    bot.report_not_first_buy(db.followers(WALLET), TOKEN1, "AAA", 42.0, 101, "0xtx2", "rh", {"price_usd": 0.001})
    assert len(bot.tg.sent) == before


def test_source_stats_and_screen(tmp_path):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001")
    db.add_trader(1, WALLET, "alpha", source="fomo")
    db.add_trader(1, WALLET2, "beta", source="manual")
    add_position(db, 1, "rh", TOKEN1, "AAA", WALLET, "alpha", status="closed", cost=10.0, realized=25.0, source="wallet")
    add_position(db, 1, "sol", TOKEN2, "BBB", WALLET2, "beta", status="closed", cost=10.0, realized=5.0,
                 source="confluence")

    be, bi = db.source_stats(1)
    assert be["wallet"][0] == 1 and be["confluence"][0] == 1
    assert round(be["wallet"][2], 2) == 14.98 and be["wallet"][1] == 1   # +25 -10 -0.02 gas, a win
    assert round(be["confluence"][2], 2) == -5.02 and be["confluence"][1] == 0
    assert bi["fomo"][0] == 1 and bi["manual"][0] == 1

    text, kb = bot.ui.by_source(db.user(1))
    assert "FOMO" in text and "совпадение" in text and "вручную" in text
