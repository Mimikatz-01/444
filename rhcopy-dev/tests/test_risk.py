"""Этап 4: insider / launch-risk filter — report model, parsers, the judge gate and the cache."""
import json

import pytest

import rhcopy.risk as riskmod
from conftest import ROOT, make_bot
from rhcopy.bot import Skip
from rhcopy.risk import MadeOnSolProvider, RiskReport, SolanaHeuristics, bonding_curve_pda

FIX = ROOT / "tests" / "fixtures"


def test_riskreport_merge_summary_json():
    a = RiskReport("sol", "M", bundle_pct=12.0, source="madeonsol")
    b = RiskReport("sol", "M", dev_pct=3.0, top10_pct=45.0, source="solana-rpc")
    a.merge(b)
    assert a.bundle_pct == 12.0 and a.dev_pct == 3.0 and a.top10_pct == 45.0
    assert "madeonsol" in a.source and "solana-rpc" in a.source
    assert "пачка 12%" in a.summary() and "топ-10 45%" in a.summary()
    back = RiskReport.from_json("sol", "M", a.to_json())
    assert back.bundle_pct == 12.0 and back.top10_pct == 45.0 and back.known()
    assert not RiskReport("sol", "M").known()


class FakeMos:
    configured = True

    def __init__(self, bundle=None, deployer=None):
        self._b, self._d = bundle, deployer

    def token_bundle(self, mint):
        return self._b

    def token_deployer(self, mint):
        return self._d


def test_madeonsol_provider_parses_bundle_and_rugs():
    bundle = json.loads((FIX / "madeonsol_bundle.json").read_text())["body"]  # client already unwraps {status,body}
    prov = MadeOnSolProvider(FakeMos(bundle=bundle, deployer={"deployer": {"rugs": 3, "tier": "risky"}}))
    r = prov.report("sol", "fCUBpdeRn76xfRaa4UPHDauGeRG3EvMB3MjuRgdpump", {}, deadline=9e18)
    assert r.bundle_pct == round(bundle["bundle"]["held_pct_of_supply"] * 100, 2)
    assert r.dev_rugs == 3 and r.source == "madeonsol"


def test_madeonsol_deployer_without_rug_count_is_none():
    dep = json.loads((FIX / "madeonsol_deployer.json").read_text())["body"]
    prov = MadeOnSolProvider(FakeMos(bundle=None, deployer=dep))
    r = prov.report("sol", "x", {}, deadline=9e18)
    assert r.dev_rugs is None  # this deployer record carries no rug count


class FakeSolRPC:
    def __init__(self, accounts):
        self.accounts = accounts   # pubkey -> getAccountInfo "value"

    def call(self, method, params):
        if method == "getAccountInfo":
            return {"value": self.accounts.get(params[0])}
        raise Exception(f"{method} not supported")


def test_solana_heuristics_reads_pump_creator():
    curves = json.loads((FIX / "pump_bonding_curves.json").read_text())
    mint, entry = next(iter(curves.items()))
    assert bonding_curve_pda(mint) == entry["pda"]   # PDA derivation matches pump.fun
    rpc = FakeSolRPC({entry["pda"]: {"data": entry["account"]["data"]}})
    assert SolanaHeuristics(rpc)._creator(mint) == entry["creator"]


def gate_bot(db_path, mode="soft"):
    bot = make_bot(db_path)
    bot.db.update_user(1, chat_id="1001")
    u = bot.db.user(1)
    if mode != "soft":
        bot.set_override(u, "gates.insider_check", json.dumps(mode))
    return bot, bot.db.user(1)


def test_gate_skips_on_proven_violation(tmp_path, monkeypatch):
    bot, u = gate_bot(tmp_path / "bot.db")
    g = bot.ucfg(u)["gates"]
    monkeypatch.setattr(riskmod, "assess", lambda *a, **k: RiskReport("sol", "T", bundle_pct=40.0))
    with pytest.raises(Skip) as e:
        bot.insider_gate(u, g, "sol", "T", {}, {})
    assert e.value.reason == "insider_bundle" and e.value.details["bundle_pct"] == 40.0


def test_gate_soft_passes_unknown_strict_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(riskmod, "assess", lambda *a, **k: RiskReport("sol", "T"))  # all None
    bot, u = gate_bot(tmp_path / "soft.db", "soft")
    d = {}
    bot.insider_gate(u, bot.ucfg(u)["gates"], "sol", "T", {}, d)   # soft: no raise
    assert d["risk"] == "нет данных"
    bot2, u2 = gate_bot(tmp_path / "strict.db", "strict")
    with pytest.raises(Skip) as e:
        bot2.insider_gate(u2, bot2.ucfg(u2)["gates"], "sol", "T", {}, {})
    assert e.value.reason == "risk_unknown"


def test_gate_dev_rugger_and_off(tmp_path, monkeypatch):
    bot, u = gate_bot(tmp_path / "bot.db")
    monkeypatch.setattr(riskmod, "assess", lambda *a, **k: RiskReport("sol", "T", dev_rugs=2))
    with pytest.raises(Skip) as e:
        bot.insider_gate(u, bot.ucfg(u)["gates"], "sol", "T", {}, {})
    assert e.value.reason == "dev_rugger"
    # off -> never even calls assess
    bot.set_override(bot.db.user(1), "gates.insider_check", json.dumps("off"))
    monkeypatch.setattr(riskmod, "assess", lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    bot.insider_gate(bot.db.user(1), bot.ucfg(bot.db.user(1))["gates"], "sol", "T", {}, {})


def test_risk_cache_roundtrip_and_ttl(tmp_path):
    bot = make_bot(tmp_path / "bot.db")
    bot.db.put_risk("sol", "M", {"bundle_pct": 5.0, "source": "x"})
    assert bot.db.get_risk("sol", "M")["bundle_pct"] == 5.0
    assert bot.db.get_risk("sol", "M", ttl=-1) is None     # expired
    assert bot.db.get_risk("sol", "OTHER") is None


@pytest.mark.network
def test_live_risk_read_only(tmp_path):
    """Read-only: assess two known tokens against real RPC. Fields may be None depending on the
    node, but it must return a report without errors and cache it."""
    bot = make_bot(tmp_path / "bot.db")
    for chain, token in (("sol", "5GDX5fJTQns4arM1J94wjxdA8KXFsV5qW46MLFpHpump"),
                         ("eth", "0x6982508145454ce325ddbe47a25d4ec3d2311933")):
        rep = riskmod.assess(bot, chain, token, {})
        assert isinstance(rep, RiskReport) and rep.chain == chain
        assert bot.db.get_risk(chain, token) is not None   # cached
