"""Этап 2: wallet import (paste + MadeOnSol leaderboard) and the watcher scaling helpers."""
import json

from conftest import ROOT, make_bot
from rhcopy.sol import SolWatcher
from rhcopy.sources import MadeOnSol, filter_kols, import_stats, parse_wallets

FIX = ROOT / "tests" / "fixtures"
EVM = "0xddd462bb053b57d5d73c9615e11a7284cfee9233"
SOL = "DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6"
SOL2 = "4XPvoZQrTnGZuTiYqMm2z6JbeHMtsZQfJyYMJCwgMKot"


def test_parse_wallets_links_labels_and_dedup():
    text = (f"Cented {EVM}\n"
            f"{SOL} solguy\n"
            f"https://gmgn.ai/sol/address/{SOL}\n"           # same wallet via a link -> deduped
            f"https://kolscan.io/account/{SOL2}\n"
            f"{EVM}\n")                                       # duplicate EVM -> deduped
    items = parse_wallets(text)
    by = {x["address"]: x for x in items}
    assert len(items) == 3
    assert by[EVM]["kind"] == "evm" and by[EVM]["label"] == "Cented" and by[EVM]["source"] == "paste"
    assert by[SOL]["kind"] == "sol" and by[SOL]["label"] == "solguy"
    assert by[SOL2]["source"] == "kolscan" and by[SOL2]["kind"] == "sol"
    assert import_stats(items) == {"total": 3, "sol": 2, "evm": 1}


def test_parse_wallets_source_by_domain():
    items = parse_wallets(f"https://gmgn.ai/sol/token/{SOL}\nhttps://madeonsol.com/kol-tracker/{SOL2}")
    src = {x["address"]: x["source"] for x in items}
    assert src[SOL] == "gmgn" and src[SOL2] == "madeonsol"


def test_madeonsol_leaderboard_parse_and_filter(monkeypatch):
    data = json.loads((FIX / "madeonsol_leaderboard_7d.json").read_text())
    mos = MadeOnSol(api_key="x")
    monkeypatch.setattr(mos, "_get", lambda path, params=None: (200, data))
    rows = mos.leaderboard()
    assert rows and all(r["wallet"] and "win_rate" in r and "trades" in r for r in rows)
    cented = next(r for r in rows if r["name"] == "Cented")
    assert cented["trades"] == 2746 and abs(cented["win_rate"] - 51.35) < 0.01
    picked = filter_kols(rows, min_winrate=50, min_trades=100, top=5)
    assert 0 < len(picked) <= 5
    assert all(r["win_rate"] >= 50 and r["trades"] >= 100 for r in picked)
    assert picked == sorted(picked, key=lambda r: r["pnl"], reverse=True)


def test_madeonsol_unavailable_without_key():
    mos = MadeOnSol(api_key=None)
    assert not mos.configured and mos.leaderboard() is None


def test_madeonsol_pro_tier_is_unavailable_not_error(monkeypatch):
    mos = MadeOnSol(api_key="x")
    monkeypatch.setattr(mos, "_get", lambda path, params=None: (403, {"error": "tier_required"}))
    assert mos.leaderboard() is None and "403" in mos.last_error


def test_solwatcher_splits_into_groups():
    w = SolWatcher("wss://x", per_conn=2)
    groups = w.groups_for({"a", "b", "c", "d", "e"})
    assert len(groups) == 3 and sorted(len(g) for g in groups) == [1, 2, 2]
    assert set().union(*groups) == {"a", "b", "c", "d", "e"}   # every wallet covered exactly once
    assert w.groups_for(set()) == []


def test_import_flow_adds_traders_with_source_and_limit(tmp_path):
    bot = make_bot(tmp_path / "bot.db")
    db = bot.db
    db.update_user(1, chat_id="1001")
    items = parse_wallets(f"{EVM} alpha\n{SOL} solguy")
    bot.ui.import_preview(db.user(1), items)
    _scr, note = bot.ui.do_import(db.user(1))
    assert db.trader(1, "alpha")["source"] == "paste" and db.trader(1, "solguy")["source"] == "paste"
    assert db.pairs(SOL) == {SOL}            # Solana wallet watches its own address
    assert not db.pairs(EVM)                 # non-FOMO EVM wallet: empty pair is fine
    assert "добавлено 2" in note

    # the per-user limit caps additions
    bot.base_cfg["sources"]["max_traders_per_user"] = 2
    bot.ui.import_preview(db.user(1), parse_wallets(f"{SOL2} three\n0xaaaa000000000000000000000000000000000004 four"))
    _scr, note = bot.ui.do_import(db.user(1))
    assert not db.trader(1, "three") and "лимит" in note
