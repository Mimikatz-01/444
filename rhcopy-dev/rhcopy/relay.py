"""Relay (relay.link) client: same-chain swap quotes and FOMO fill provenance.

FOMO executes its users' Robinhood Chain buys as Relay requests: USDC on Solana in,
token delivered to the trader's EVM wallet. The Relay record of such a fill carries
referrer "fomo", an origin tx on Solana and that tx's signer. A third party can name
any recipient, but cannot sign a Solana deposit from the trader's own Solana wallet,
so signer == trader's paired Solana wallet is what proves the buy is genuine.
"""
import time
from collections import Counter
from dataclasses import dataclass, field

import requests

API = "https://api.relay.link"
CHAIN_ID = 4663
SOLANA_CHAIN_ID = 792703809
APPROVAL_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
ERC20_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
RELAY_CONTRACTS = {APPROVAL_PROXY, ERC20_ROUTER}


@dataclass
class Quote:
    request_id: str
    out_raw: int
    min_out_raw: int
    in_usd: float
    out_usd: float
    impact_pct: float          # negative = you lose this much vs mid
    gas_usd: float
    relayer_usd: float
    steps: list = field(default_factory=list)

    def tx_items(self, step_id):
        for s in self.steps:
            if s.get("id") == step_id and s.get("kind") == "transaction":
                return [i["data"] for i in s.get("items", [])]
        return []


@dataclass
class FillRecord:
    referrer: str
    origin_chain: int
    signer: str
    recipient: str
    in_usd: float


class Relay:
    def __init__(self, api_key=None, timeout=15, log=None):
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "accept": "application/json",
                               "User-Agent": "rhcopy/0.1"})
        if api_key:
            self.s.headers["Authorization"] = f"Bearer {api_key}"
        self.timeout = timeout
        self.log = log
        self.last_error = ""
        self.solvers = set()          # Robinhood Chain (kept for older call sites)
        self.solvers_by, self.contracts_by = {}, {}
        self._load_solvers()

    def _load_solvers(self):
        try:
            r = self.s.get(f"{API}/chains", timeout=self.timeout).json()
            for c in r.get("chains", []):
                cid = c.get("id")
                self.solvers_by[cid] = {a.lower() for a in c.get("solverAddresses", []) or []}
                addrs = set()
                for v in (c.get("contracts") or {}).values():  # flat addresses and nested versions
                    for a in (v.values() if isinstance(v, dict) else [v]):
                        if isinstance(a, str) and a.startswith("0x"):
                            addrs.add(a.lower())
                self.contracts_by[cid] = addrs
                if cid == CHAIN_ID:
                    self.solvers = self.solvers_by[cid]
        except Exception as e:
            if self.log:
                self.log.warning("relay chains fetch failed: %s", e)

    def solvers_of(self, chain_id):
        return self.solvers_by.get(chain_id) or (self.solvers if chain_id == CHAIN_ID else set())

    def contracts_of(self, chain_id):
        return self.contracts_by.get(chain_id) or RELAY_CONTRACTS

    def quote(self, user, cin, cout, amount_raw, slippage_bps=None, chain_id=CHAIN_ID):
        body = {"user": user, "recipient": user, "originChainId": chain_id, "destinationChainId": chain_id,
                "originCurrency": cin, "destinationCurrency": cout, "amount": str(int(amount_raw)),
                "tradeType": "EXACT_INPUT"}
        if slippage_bps is not None:
            body["slippageTolerance"] = str(int(slippage_bps))
        try:
            r = self.s.post(f"{API}/quote/v2", json=body, timeout=self.timeout)
            j = r.json()
        except Exception as e:
            self.last_error = f"relay quote: {e}"
            return None
        if r.status_code != 200 or "details" not in j:
            self.last_error = f"relay quote {r.status_code}: {str(j)[:200]}"
            return None
        d, fees = j["details"], j.get("fees", {})

        def usd(x):
            try:
                return float(x or 0)
            except (TypeError, ValueError):
                return 0.0
        co = d.get("currencyOut", {})
        return Quote(
            request_id=j.get("requestId", ""),
            out_raw=int(co.get("amount") or 0),
            min_out_raw=int(co.get("minimumAmount") or 0),
            in_usd=usd(d.get("currencyIn", {}).get("amountUsd")),
            out_usd=usd(co.get("amountUsd")),
            impact_pct=usd((d.get("totalImpact") or {}).get("percent")),
            gas_usd=usd((fees.get("gas") or {}).get("amountUsd")),
            relayer_usd=usd((fees.get("relayer") or {}).get("amountUsd")),
            steps=j.get("steps", []),
        )

    def fill_record(self, tx_hash):
        """Relay record for an on-chain fill, or None if Relay has none (yet)."""
        try:
            j = self.s.get(f"{API}/requests/v2", params={"hash": tx_hash}, timeout=self.timeout).json()
        except Exception:
            return None
        reqs = j.get("requests") or []
        if not reqs:
            return None
        return self._parse(reqs[0])

    @staticmethod
    def _parse(q):
        d = q.get("data") or {}
        ins = d.get("inTxs") or [{}]
        cin = ((d.get("metadata") or {}).get("currencyIn") or {})
        try:
            in_usd = float(cin.get("amountUsd") or 0)
        except (TypeError, ValueError):
            in_usd = 0.0
        return FillRecord(
            referrer=(q.get("referrer") or ""),
            origin_chain=int(ins[0].get("chainId") or 0),
            signer=((ins[0].get("data") or {}).get("signer") or ""),
            recipient=(q.get("recipient") or "").lower(),
            in_usd=in_usd,
        )

    def wait_fill_record(self, tx_hash, wait_s):
        t0 = time.time()
        while True:
            rec = self.fill_record(tx_hash)
            if rec or time.time() - t0 >= wait_s:
                return rec
            time.sleep(0.5)

    def learn_pair(self, evm_wallet, limit=50):
        """Solana signers of the wallet's recent FOMO fills -> ([(signer, votes)...] most common first, total)."""
        w = evm_wallet.lower()
        try:
            j = self.s.get(f"{API}/requests/v2", params={"user": w, "limit": limit}, timeout=self.timeout).json()
        except Exception:
            return [], 0
        votes = Counter()
        for q in j.get("requests") or []:
            rec = self._parse(q)
            if rec.referrer == "fomo" and rec.origin_chain == SOLANA_CHAIN_ID and rec.recipient == w and rec.signer:
                votes[rec.signer] += 1
        return votes.most_common(), sum(votes.values())
