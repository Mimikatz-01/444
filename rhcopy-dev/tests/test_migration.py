"""A database and a config.yaml from the previous version must open and keep working."""
import shutil

import pytest

from conftest import ROOT, load_cfg, load_sql, make_bot


def cols(db, table):
    return {r[1] for r in db.c.execute(f"PRAGMA table_info({table})")}


def check_opened(bot):
    assert {"muted", "eth_live", "sol_key", "sol_live", "live_chains"} <= cols(bot.db, "users")
    assert {"chain", "source"} <= cols(bot.db, "positions")
    assert "source" in cols(bot.db, "traders")
    for u in bot.db.users(active_only=False):
        assert bot.status_text(u)
        assert bot.ui.main(u)[0]


def test_prev_version_db_and_old_config(tmp_path):
    path = tmp_path / "bot.db"
    load_sql(path, "prev_version.sql")
    bot = make_bot(path, cfg=load_cfg("config.yaml"))  # old config: no chains / ethereum / solana sections
    check_opened(bot)
    assert len(bot.db.traders(2)) == 2 and bot.db.pairs("0xddd462bb053b57d5d73c9615e11a7284cfee9233")
    assert len(bot.db.positions(2)) == 1 and bot.db.closed_positions(2)
    assert bot.get_key(bot.db.user(2), "sizing.ticket_usd") == 20
    assert "eth" in bot.nets and bot.sol_enabled
    check_opened(make_bot(path, cfg=load_cfg("config.yaml")))  # reopening is idempotent


def test_data_dev_copy_opens(tmp_path):
    src = ROOT / "data-dev" / "bot.db"
    if not src.exists():
        pytest.skip("data-dev/bot.db is not present")
    shutil.copy(src, tmp_path / "bot.db")
    check_opened(make_bot(tmp_path / "bot.db", cfg=load_cfg("config.yaml")))
