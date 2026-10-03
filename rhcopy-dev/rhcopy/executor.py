"""Buy/sell through Relay. Paper mode = real quotes, simulated fills, nothing is sent."""
from dataclasses import dataclass

from eth_account import Account
from eth_utils import to_checksum_address

from .chain import MAX_UINT, Chain, pad_addr
from .relay import APPROVAL_PROXY, CHAIN_ID

DUMMY_USER = "0x000000000000000000000000000000000000dEaD"


class ExecError(Exception):
    pass


@dataclass
class Fill:
    tokens_raw: int     # tokens bought / sold
    usd: float          # USDG spent (buy) or received (sell)
    gas_usd: float
    tx: str
    quote_impact_pct: float = 0.0


class Executor:
    def __init__(self, chain: Chain, relay, cfg, private_key=None, eth_price=lambda: 0.0, log=None,
                 chain_id=CHAIN_ID, approval_proxy=APPROVAL_PROXY):
        self.chain, self.relay, self.cfg, self.log = chain, relay, cfg, log
        self.chain_id, self.approval_proxy = chain_id, approval_proxy
        self.live = bool(cfg["live"])
        self.usdg = cfg["usdg"].lower()
        self.usd_dec = int(cfg.get("usd_decimals", 6))  # stablecoin decimals (BNB Chain USDT = 18)
        self.eth_price = eth_price
        self.acct = Account.from_key(private_key) if private_key else None
        if self.live and not self.acct:
            raise SystemExit("live: true needs PRIVATE_KEY in .env")
        self.addr = self.acct.address if self.acct else DUMMY_USER

    # ---------- public ----------
    def buy(self, token, usd, slippage_bps):
        amount = int(round(usd * 10 ** self.usd_dec))  # stablecoin units (6 decimals, 18 on BNB Chain)
        q = self.relay.quote(self.addr, self.usdg, token, amount, slippage_bps, chain_id=self.chain_id)
        if not q or q.out_raw <= 0:
            raise ExecError(self.relay.last_error or "no route")
        if not self.live:
            return Fill(q.out_raw, usd, q.gas_usd, "paper", q.impact_pct)
        self._ensure_allowance(self.usdg, amount)
        tb0 = self.chain.erc20_balance(token, self.addr)
        ub0 = self.chain.erc20_balance(self.usdg, self.addr)
        h, gas_eth = self._send_step(q, "swap")
        got = self.chain.erc20_balance(token, self.addr) - tb0
        spent = (ub0 - self.chain.erc20_balance(self.usdg, self.addr)) / 10 ** self.usd_dec
        if got <= 0:
            raise ExecError(f"swap {h} landed but no tokens arrived")
        # pre-approve the token so every later exit is a single transaction
        try:
            self._ensure_allowance(token, MAX_UINT // 2)
        except ExecError as e:
            if self.log:
                self.log.warning("pre-approve %s failed: %s", token, e)
        return Fill(got, spent, gas_eth * self.eth_price(), h, q.impact_pct)

    def sell(self, token, tokens_raw, slippage_bps):
        q = self.relay.quote(self.addr, token, self.usdg, tokens_raw, slippage_bps, chain_id=self.chain_id)
        if not q or q.out_raw <= 0:
            raise ExecError(self.relay.last_error or "no route")
        if not self.live:
            return Fill(tokens_raw, q.out_raw / 10 ** self.usd_dec, q.gas_usd, "paper", q.impact_pct)
        self._ensure_allowance(token, tokens_raw)
        ub0 = self.chain.erc20_balance(self.usdg, self.addr)
        tb0 = self.chain.erc20_balance(token, self.addr)
        h, gas_eth = self._send_step(q, "swap")
        got = (self.chain.erc20_balance(self.usdg, self.addr) - ub0) / 10 ** self.usd_dec
        sold = tb0 - self.chain.erc20_balance(token, self.addr)
        return Fill(sold, got, gas_eth * self.eth_price(), h, q.impact_pct)

    def transfer_erc20(self, token, to, amount_raw):
        """Withdraw a token to an address the user chose."""
        data = "0xa9059cbb" + pad_addr(to)[2:] + hex(int(amount_raw))[2:].rjust(64, "0")
        return self._send(token, data)[0]

    def eth_reserve(self, to):
        """Wei that must stay behind to pay for sending ETH to `to`."""
        est = int(self.chain.call("eth_estimateGas", [{"from": self.addr, "to": to_checksum_address(to),
                                                       "value": "0x1"}]), 16)
        return int(est * 1.3) * self.chain.gas_price() * 2

    def send_eth(self, to, amount_wei):
        return self._send(to, "0x", value=int(amount_wei))[0]

    def balances(self):
        if not self.live:
            return None
        return {"usdg": self.chain.erc20_balance(self.usdg, self.addr) / 10 ** self.usd_dec,
                "eth": self.chain.eth_balance(self.addr) / 1e18}

    # ---------- internals ----------
    def _ensure_allowance(self, token, amount):
        if self.chain.allowance(token, self.addr, self.approval_proxy) >= amount:
            return
        self._send(token, Chain.approve_data(self.approval_proxy), gas=None)

    def _send_step(self, q, step_id):
        items = q.tx_items(step_id)
        if not items:
            raise ExecError(f"quote has no '{step_id}' step")
        it = items[0]
        if int(it.get("chainId", self.chain_id)) != self.chain_id:
            raise ExecError("quote step is for another chain")
        return self._send(it["to"], it["data"], value=int(it.get("value") or 0),
                          gas=int(int(it.get("gas") or 0) * 1.25) or None,
                          max_fee=int(it.get("maxFeePerGas") or 0),
                          prio=int(it.get("maxPriorityFeePerGas") or 0))

    def _send(self, to, data, value=0, gas=None, max_fee=0, prio=0):
        gp = self.chain.gas_price()
        tx = {"type": 2, "chainId": self.chain_id, "nonce": self.chain.nonce(self.addr),
              "to": to_checksum_address(to), "data": data, "value": int(value),
              "maxFeePerGas": max(int(max_fee or 0), gp * 2), "maxPriorityFeePerGas": int(prio or 0)}
        if not gas:
            est = self.chain.call("eth_estimateGas", [{"from": self.addr, "to": tx["to"], "data": data,
                                                       "value": hex(int(value))}])
            gas = int(int(est, 16) * 1.3)
        tx["gas"] = int(gas)
        signed = self.acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        raw_hex = raw.hex() if isinstance(raw, (bytes, bytearray)) else str(raw)
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        h = self.chain.send_raw(raw_hex)
        rc = self.chain.wait_receipt(h, timeout=int(self.cfg["execution"].get("receipt_timeout_s", 60)))
        if not rc:
            raise ExecError(f"no receipt for {h}")
        gas_eth = int(rc["gasUsed"], 16) * int(rc.get("effectiveGasPrice") or "0x0", 16) / 1e18
        if int(rc["status"], 16) != 1:
            raise ExecError(f"tx {h} reverted")
        return h, gas_eth
