"""DexScreener market data for Robinhood Chain tokens (liquidity, age, flow, price)."""
import time

import requests

API = "https://api.dexscreener.com/tokens/v1"


class Market:
    def __init__(self, chain="robinhood", timeout=10, ttl=5.0):
        self.chain = chain
        self.s = requests.Session()
        self.s.headers.update({"accept": "application/json", "User-Agent": "rhcopy/0.1"})
        self.timeout = timeout
        self.ttl = ttl
        self._cache = {}

    def _fetch(self, tokens):
        out = {}
        for i in range(0, len(tokens), 30):
            chunk = tokens[i:i + 30]
            try:
                pairs = self.s.get(f"{API}/{self.chain}/{','.join(chunk)}", timeout=self.timeout).json()
            except Exception:
                continue
            if not isinstance(pairs, list):
                continue
            for p in pairs:
                base = self._k((p.get("baseToken") or {}).get("address", ""))
                if base not in [self._k(c) for c in chunk]:
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if base in out and out[base]["liquidity_usd"] >= liq:
                    continue  # keep the deepest pool
                tx24 = (p.get("txns") or {}).get("h24") or {}
                out[base] = {
                    "price_usd": float(p.get("priceUsd") or 0),
                    "liquidity_usd": liq,
                    "mcap_usd": float(p.get("marketCap") or p.get("fdv") or 0),
                    "pair_created_ms": int(p.get("pairCreatedAt") or 0),
                    "buys_h24": int(tx24.get("buys") or 0),
                    "sells_h24": int(tx24.get("sells") or 0),
                    "symbol": (p.get("baseToken") or {}).get("symbol", ""),
                    "dex": p.get("dexId", ""),
                    "pair": p.get("pairAddress", ""),
                }
        now = time.time()
        for t, v in out.items():
            self._cache[t] = (now, v)
        return out

    def _k(self, t):
        return t if self.chain == "solana" else t.lower()  # Solana addresses are case-sensitive

    def info(self, token, fresh=False):
        t = self._k(token)
        hit = self._cache.get(t)
        if hit and not fresh and time.time() - hit[0] < self.ttl:
            return hit[1]
        return self._fetch([t]).get(t)

    def prices(self, tokens):
        toks = [self._k(t) for t in tokens]
        data = self._fetch(toks) if toks else {}
        return {t: v["price_usd"] for t, v in data.items() if v["price_usd"] > 0}
