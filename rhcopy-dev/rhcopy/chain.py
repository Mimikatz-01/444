"""Minimal JSON-RPC client for Robinhood Chain (EVM, chain id 4663)."""
import json
import time

import requests

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SWAP_TOPICS = {
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",  # v2 Swap
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",  # v3 Swap
    "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f",  # v4 Swap
}
MAX_UINT = (1 << 256) - 1


class RPCError(Exception):
    pass


def pad_addr(a: str) -> str:
    return "0x" + "0" * 24 + a.lower()[2:]


def topic_addr(t: str) -> str:
    return "0x" + t[-40:]


class Chain:
    def __init__(self, urls, timeout=20, log=None):
        self.urls = [u for u in urls if u]
        self.i = 0
        self.timeout = timeout
        self.log = log
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "rhcopy/0.1"})
        self._code, self._dec, self._sym, self._name, self._ts = {}, {}, {}, {}, {}

    # ---------- transport ----------
    def _post(self, payload):
        """Primary first on every call (a fallback node may lag the head); 2 tries each, backoff on 429."""
        last = None
        for url in self.urls:
            for attempt in range(2):
                try:
                    r = self.s.post(url, data=json.dumps(payload), timeout=self.timeout)
                    if r.status_code == 429:
                        last = RPCError(f"429 from {url}")
                        time.sleep(1.0 + attempt)
                        continue
                    r.raise_for_status()
                    return r.json()
                except Exception as e:  # network / 4xx / 5xx
                    last = e
                    time.sleep(0.3)
            if self.log and len(self.urls) > 1:
                self.log.warning("rpc %s failed (%s), trying next", url, last)
        raise RPCError(f"all RPCs failed: {last}")

    def call(self, method, params):
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if "error" in r:
            raise RPCError(f"{method}: {r['error']}")
        return r["result"]

    def batch(self, calls):
        """calls: [(method, params)] -> list of results (RPCError instances on per-call errors)."""
        if not calls:
            return []
        payload = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
        r = self._post(payload)
        if isinstance(r, dict):
            raise RPCError(str(r.get("error", r)))
        out = [None] * len(calls)
        for x in r:
            out[x["id"]] = RPCError(str(x["error"])) if "error" in x else x.get("result")
        return out

    # ---------- basics ----------
    def chain_id(self):
        return int(self.call("eth_chainId", []), 16)

    def head(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, n):
        if n not in self._ts:
            b = self.call("eth_getBlockByNumber", [hex(n), False])
            self._ts[n] = int(b["timestamp"], 16)
            if len(self._ts) > 5000:
                self._ts.clear()
        return self._ts[n]

    def is_contract(self, addr):
        a = addr.lower()
        if a not in self._code:
            self._code[a] = len(self.call("eth_getCode", [a, "latest"])) > 2
        return self._code[a]

    def tx(self, h):
        return self.call("eth_getTransactionByHash", [h])

    def receipt(self, h):
        return self.call("eth_getTransactionReceipt", [h])

    def gas_price(self):
        return int(self.call("eth_gasPrice", []), 16)

    def eth_balance(self, addr):
        return int(self.call("eth_getBalance", [addr, "latest"]), 16)

    def nonce(self, addr):
        return int(self.call("eth_getTransactionCount", [addr, "pending"]), 16)

    def send_raw(self, raw_hex):
        return self.call("eth_sendRawTransaction", [raw_hex])

    def wait_receipt(self, h, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                r = self.receipt(h)
                if r:
                    return r
            except RPCError:
                pass
            time.sleep(0.25)
        return None

    # ---------- ERC-20 ----------
    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def erc20_balance(self, token, owner, block="latest"):
        r = self.eth_call(token, "0x70a08231" + pad_addr(owner)[2:], block)
        return int(r, 16) if r and r != "0x" else 0

    def allowance(self, token, owner, spender):
        r = self.eth_call(token, "0xdd62ed3e" + pad_addr(owner)[2:] + pad_addr(spender)[2:])
        return int(r, 16) if r and r != "0x" else 0

    def decimals(self, token):
        t = token.lower()
        if t not in self._dec:
            try:
                r = self.eth_call(t, "0x313ce567")
                self._dec[t] = int(r, 16) if r and r != "0x" else 18
            except RPCError:
                self._dec[t] = 18
        return self._dec[t]

    def _string(self, token, selector):
        try:
            r = self.eth_call(token, selector)
        except RPCError:
            return ""
        if not r or r == "0x":
            return ""
        b = bytes.fromhex(r[2:])
        try:
            if len(b) >= 64:  # ABI-encoded string
                ln = int.from_bytes(b[32:64], "big")
                return b[64:64 + ln].decode("utf-8", "ignore")
            return b.rstrip(b"\x00").decode("utf-8", "ignore")  # bytes32
        except Exception:
            return ""

    def symbol(self, token):
        t = token.lower()
        if t not in self._sym:
            self._sym[t] = self._string(t, "0x95d89b41") or t[:8]
        return self._sym[t]

    def name(self, token):
        t = token.lower()
        if t not in self._name:
            self._name[t] = self._string(t, "0x06fdde03")
        return self._name[t]

    @staticmethod
    def approve_data(spender, amount=MAX_UINT):
        return "0x095ea7b3" + pad_addr(spender)[2:] + hex(amount)[2:].rjust(64, "0")
