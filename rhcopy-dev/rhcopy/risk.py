"""Insider / launch-risk filter (Этап 4).

A token is scored just before we would buy it: how much of the supply a launch bundle grabbed,
how much the creator holds, how concentrated the top holders are, whether the creator has rugged
before. Providers are tried in order and their findings merged; any field that cannot be determined
stays None. Results are cached (DB table token_risk, 30-minute TTL) because launch metrics barely
move and the data sources have tight rate limits.

Providers:
- MadeOnSolProvider (Solana, needs MADEONSOL_API_KEY): bundle stats and creator reputation. 402/403
  mean "unavailable", not an error.
- SolanaHeuristics: pump.fun bonding-curve creator, top-holder concentration, creator holding.
- EvmHeuristics: contract creator (Etherscan V2 getcontractcreation, free on supported chains) and
  the creator's token balance share.

madeonsol.com and the live chains are unreachable from the dev sandbox, so the parsers are covered
by fixture tests and the on-chain paths degrade to None; a @network test exercises them for real."""
import base64
import time
from dataclasses import dataclass, field

import requests
from solders.pubkey import Pubkey

PUMP_PROGRAM = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
BURN = "1nc1nerator11111111111111111111111111111111"
ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAINS = {"eth": 1, "bsc": 56, "base": 8453}  # Robinhood uses a separate explorer API
METRICS = ("bundle_pct", "dev_pct", "top10_pct", "insider_pct", "dev_rugs")


@dataclass
class RiskReport:
    chain: str
    token: str
    bundle_pct: float = None      # share of supply a launch bundle bought
    dev_pct: float = None         # share held by the creator
    top10_pct: float = None       # top-10 holders (pools / curves / burns excluded)
    insider_pct: float = None     # share in wallets tied to the creator
    dev_rugs: int = None          # the creator's past rugs, if the source knows
    source: str = ""
    notes: list = field(default_factory=list)

    def merge(self, other):
        for k in METRICS:
            if getattr(self, k) is None and getattr(other, k) is not None:
                setattr(self, k, getattr(other, k))
        self.notes += other.notes
        if other.source:
            self.source = "+".join(s for s in (self.source, other.source) if s)
        return self

    def known(self):
        return any(getattr(self, k) is not None for k in METRICS)

    def summary(self):
        p = []
        if self.bundle_pct is not None:
            p.append(f"пачка {self.bundle_pct:.0f}%")
        if self.dev_pct is not None:
            p.append(f"создатель {self.dev_pct:.0f}%")
        if self.top10_pct is not None:
            p.append(f"топ-10 {self.top10_pct:.0f}%")
        if self.insider_pct is not None:
            p.append(f"инсайдеры {self.insider_pct:.0f}%")
        if self.dev_rugs:
            p.append(f"раги {self.dev_rugs}")
        return " · ".join(p) or "нет данных"

    def to_json(self):
        d = {k: getattr(self, k) for k in METRICS}
        d["source"] = self.source
        return d

    @classmethod
    def from_json(cls, chain, token, d):
        return cls(chain, token, source=d.get("source", ""), **{k: d.get(k) for k in METRICS})


def bonding_curve_pda(mint):
    return str(Pubkey.find_program_address([b"bonding-curve", bytes(Pubkey.from_string(mint))], PUMP_PROGRAM)[0])


class MadeOnSolProvider:
    """Solana launch risk from the official MadeOnSol API (bundle + deployer reputation)."""

    def __init__(self, client):
        self.client = client

    def report(self, chain, token, info, deadline):
        r = RiskReport(chain, token, source="madeonsol")
        bundle = self.client.token_bundle(token)
        if bundle:
            b = bundle.get("bundle") or bundle.get("body", {}).get("bundle") or {}
            # held_pct_of_supply is a fraction of total supply still held by the launch bundle
            pct = b.get("held_pct_of_supply")
            if pct is not None:
                r.bundle_pct = round(float(pct) * 100, 2)
        if time.time() < deadline:
            dep = self.client.token_deployer(token)
            if dep:
                d = dep.get("deployer") or {}
                rugs = d.get("rugs", d.get("rug_count", d.get("rugged_tokens")))
                if rugs is not None:
                    r.dev_rugs = int(rugs)
                if d.get("tier"):
                    r.notes.append(f"dev tier {d['tier']}")
        return r


class SolanaHeuristics:
    """Built-in Solana checks over public RPC: pump.fun creator, top-holder concentration."""

    def __init__(self, rpc):
        self.rpc = rpc

    def report(self, chain, token, info, deadline):
        r = RiskReport(chain, token, source="solana-rpc")
        if not self.rpc:
            return r
        creator = None
        try:
            creator = self._creator(token)
        except Exception as e:
            r.notes.append(f"creator: {e}")
        try:
            if time.time() < deadline:
                r.top10_pct, r.dev_pct = self._concentration(token, creator)
        except Exception as e:
            r.notes.append(f"holders: {e}")
        return r

    def _creator(self, mint):
        """pump.fun stores the creator in the bonding-curve account (offset 49, 32 bytes)."""
        v = self.rpc.call("getAccountInfo", [bonding_curve_pda(mint),
                                             {"encoding": "base64", "commitment": "confirmed"}]).get("value")
        if not v:
            return None
        raw = base64.b64decode(v["data"][0])
        return str(Pubkey.from_bytes(raw[49:81])) if len(raw) >= 81 else None

    def _concentration(self, mint, creator):
        supply = int(self.rpc.call("getTokenSupply", [mint, {"commitment": "confirmed"}])["value"]["amount"])
        if supply <= 0:
            return None, None
        largest = self.rpc.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])["value"]
        accts = [a["address"] for a in largest]
        infos = self.rpc.call("getMultipleAccounts", [accts, {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
        exclude = {BURN, bonding_curve_pda(mint)}
        owned = []  # (owner, amount) for real holder wallets only
        for a, acc in zip(largest, infos):
            data = (acc or {}).get("data")
            owner = data.get("parsed", {}).get("info", {}).get("owner") if isinstance(data, dict) else None
            if owner and owner not in exclude:
                owned.append((owner, int(a["amount"])))
        top10 = sum(amt for _, amt in sorted(owned, key=lambda x: -x[1])[:10]) / supply * 100
        dev = (sum(amt for o, amt in owned if o == creator) / supply * 100) if creator else None
        return round(top10, 2), (round(dev, 2) if dev is not None else None)


class EvmHeuristics:
    """Built-in EVM checks: contract creator via Etherscan V2 (free on supported chains) and the
    creator's token-balance share over RPC."""

    def __init__(self, net, api_key, session=None):
        self.net = net
        self.api_key = api_key
        self.s = session or requests.Session()

    def report(self, chain, token, info, deadline):
        r = RiskReport(chain, token, source="evm")
        if not self.net:
            return r
        creator = None
        try:
            creator = self._creator(chain, token)
        except Exception as e:
            r.notes.append(f"creator: {e}")
        if creator:
            try:
                supply = self._total_supply(token)
                if supply > 0:
                    bal = self.net.chain.erc20_balance(token, creator)
                    r.dev_pct = round(bal / supply * 100, 2)
            except Exception as e:
                r.notes.append(f"dev_pct: {e}")
        else:
            r.notes.append("top holders need an indexer (Etherscan V2 PRO); not checked")
        return r

    def _creator(self, chain, token):
        cid = ETHERSCAN_CHAINS.get(chain)
        if not (self.api_key and cid):
            return None
        r = self.s.get(ETHERSCAN_V2, params={"chainid": cid, "module": "contract", "action": "getcontractcreation",
                                             "contractaddresses": token, "apikey": self.api_key}, timeout=10).json()
        res = r.get("result")
        if isinstance(res, list) and res:
            return (res[0].get("contractCreator") or "").lower() or None
        return None

    def _total_supply(self, token):
        raw = self.net.chain.eth_call(token, "0x18160ddd")  # totalSupply()
        return int(raw, 16) if raw and raw != "0x" else 0


def assess(bot, chain, token, info, budget=3.0):
    """Risk report for a token, from cache or a fresh run of the chain's providers within `budget`
    seconds. Always returns a RiskReport (fields may be None)."""
    cached = bot.db.get_risk(chain, token)
    if cached is not None:
        return RiskReport.from_json(chain, token, cached)
    deadline = time.time() + float(budget)
    rep = RiskReport(chain, token)
    if chain == "sol":
        providers = []
        if getattr(bot, "madeonsol", None) and bot.madeonsol.configured:
            providers.append(MadeOnSolProvider(bot.madeonsol))
        providers.append(SolanaHeuristics(bot.sol_rpc))
    else:
        providers = [EvmHeuristics(bot.nets.get(chain), bot.env.get("ETHERSCAN_API_KEY"))]
    for p in providers:
        if time.time() >= deadline:
            rep.notes.append("time budget exceeded")
            break
        try:
            rep.merge(p.report(chain, token, info, deadline))
        except Exception as e:
            rep.notes.append(f"{type(p).__name__}: {e}")
    bot.db.put_risk(chain, token, rep.to_json())
    return rep
