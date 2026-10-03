"""Importing trader wallets.

Two ways in (Этап 2):
- `parse_wallets(text)` — a pure parser for pasted lists / CSV / links (GMGN, kolscan, solscan,
  etherscan, MadeOnSol profiles). We never scrape those sites (they block bots); the user pastes.
- `MadeOnSol` — the official MadeOnSol REST API (needs MADEONSOL_API_KEY). Used here for the KOL
  leaderboard; the risk module reuses it for bundles and deployer reputation.

Note: madeonsol.com is unreachable from the dev sandbox (egress blocked), so the HTTP calls are
written against the official response shapes saved in tests/fixtures and degrade to a clear
"unavailable" state. Base URL and auth header are overridable from the environment."""
import os
import re
import time

import requests

from .sol import is_sol_addr

EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
# address inside a known explorer / tracker link -> (regex, source label by domain)
LINK_RES = [
    (re.compile(r"gmgn\.ai/(?:\w+/)?(?:address|token)/([A-Za-z0-9]+)", re.I), "gmgn"),
    (re.compile(r"kolscan\.io/account/([A-Za-z0-9]+)", re.I), "kolscan"),
    (re.compile(r"madeonsol\.com/(?:kol-tracker|kol|wallet)/([A-Za-z0-9]+)", re.I), "madeonsol"),
    (re.compile(r"solscan\.io/account/([A-Za-z0-9]+)", re.I), "paste"),
    (re.compile(r"etherscan\.io/address/(0x[0-9a-fA-F]{40})", re.I), "paste"),
]


def _kind(addr):
    if addr.lower().startswith("0x"):
        return "evm" if len(addr) == 42 else None
    return "sol" if is_sol_addr(addr) else None


def _extract(token):
    """(address, source) from one whitespace/comma token: a known link gives a source label,
    a bare address gives source None (-> 'paste')."""
    for rx, src in LINK_RES:
        m = rx.search(token)
        if m and _kind(m.group(1)):
            return m.group(1), src
    t = token.strip().strip(",;|")
    if _kind(t):
        return t, None
    m = EVM_RE.search(token)
    if m:
        return m.group(0), None
    return None, None


def parse_wallets(text):
    """Parse a pasted blob into a deduped list of {address, label, source, kind}.

    Each line may be "ник адрес", "адрес ник", a CSV row or a link. The source label comes from
    the link domain (gmgn / kolscan / madeonsol), otherwise 'paste'."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        addrs, labels, src = [], [], None
        for tok in re.split(r"[\s,;|\t]+", line):
            if not tok:
                continue
            a, s = _extract(tok)
            if a:
                addrs.append(a)
                src = src or s
            elif not tok.startswith(("http", "0x")):
                labels.append(tok)
        for a in addrs:
            key = a.lower() if a.lower().startswith("0x") else a
            if key in seen:
                continue
            seen.add(key)
            kind = _kind(a)
            label = (" ".join(labels).strip()[:32] if len(addrs) == 1 and labels
                     else (a[:8] if kind == "evm" else a[:6]))
            out.append({"address": a, "label": label or a[:8], "source": src or "paste", "kind": kind})
    return out


def import_stats(items):
    """Counts for the preview screen."""
    return {"total": len(items),
            "sol": sum(1 for x in items if x["kind"] == "sol"),
            "evm": sum(1 for x in items if x["kind"] == "evm")}


class MadeOnSolError(Exception):
    pass


class MadeOnSol:
    """Official MadeOnSol REST client. Free tier (200 req/day): KOL leaderboard (max 50) and token
    bundles are free; token risk score is PRO (402/403 -> treated as 'unavailable', not an error).
    Responses are cached. Base URL / auth header are overridable via the environment."""

    def __init__(self, api_key=None, log=None, ttl=300.0):
        self.api_key = api_key
        self.base = (os.environ.get("MADEONSOL_API_URL") or "https://madeonsol.com/api").rstrip("/")
        self.log = log
        self.ttl = ttl
        self.last_error = ""
        self._cache = {}
        self.s = requests.Session()
        self.s.headers.update({"accept": "application/json", "User-Agent": "rhcopy/0.2"})
        if api_key:
            self.s.headers[os.environ.get("MADEONSOL_API_HEADER", "x-api-key")] = api_key

    @property
    def configured(self):
        return bool(self.api_key)

    def _get(self, path, params=None):
        """GET {base}{path}. Returns (status, body_dict) or (None, None) on a transport failure.
        Some endpoints answer with a {"status":..,"body":..} envelope; unwrap it."""
        key = (path, tuple(sorted((params or {}).items())))
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        try:
            r = self.s.get(f"{self.base}{path}", params=params, timeout=15)
            try:
                j = r.json()
            except ValueError:
                j = {}
            status = r.status_code
            if isinstance(j, dict) and "status" in j and "body" in j:
                status, j = j["status"], j["body"]
            res = (status, j)
        except Exception as e:
            self.last_error = f"madeonsol: {e}"
            return None, None
        self._cache[key] = (time.time(), res)
        return res

    def leaderboard(self, period="7d", limit=50):
        """Top KOL wallets. Returns a list of normalized dicts, or None if unavailable."""
        if not self.configured:
            self.last_error = "no MADEONSOL_API_KEY"
            return None
        status, body = self._get("/kol/leaderboard", {"period": period, "limit": int(limit)})
        if status is None:
            return None
        if status in (401, 402, 403):
            self.last_error = f"madeonsol {status}: tier/ключ"
            return None
        rows = body.get("leaderboard", body) if isinstance(body, dict) else body
        return [self._kol(r) for r in rows if isinstance(r, dict) and r.get("wallet")] if rows else []

    @staticmethod
    def _kol(r):
        return {"wallet": r.get("wallet"), "name": r.get("name") or (r.get("wallet") or "")[:6],
                "win_rate": float(r.get("win_rate") or 0), "pnl": float(r.get("pnl") or 0),
                "roi": float(r.get("avg_roi") or 0),
                "trades": int(r.get("sell_count") or r.get("buy_count") or r.get("trades") or 0)}

    def token_bundle(self, mint):
        """Launch-bundle stats for a mint (free tier). Returns body dict or None."""
        if not self.configured:
            return None
        status, body = self._get(f"/tokens/{mint}/bundle")
        return body if status == 200 else None

    def token_deployer(self, mint):
        """Deployer reputation for a mint's creator (free tier). Returns body dict or None."""
        if not self.configured:
            return None
        status, body = self._get(f"/tokens/{mint}/deployer")
        return body if status == 200 else None


def filter_kols(rows, min_winrate=0, min_trades=0, top=20):
    """Apply the import filters client-side (the free leaderboard can't filter server-side)."""
    keep = [r for r in rows if r["win_rate"] >= float(min_winrate) and r["trades"] >= int(min_trades)]
    keep.sort(key=lambda r: r["pnl"], reverse=True)
    return keep[:int(top)]
