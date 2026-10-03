"""Solana side of the bot.

Detection: one WebSocket (logsSubscribe per trader wallet) -> signature queue -> getTransaction ->
token balance changes of the trader's own wallet. On Solana the trader signs his own swap, so a
buy counts only if his wallet is a signer: tokens pushed into the wallet by someone else are ignored.

Execution: Jupiter Swap API V2 (/order + /execute). Jupiter finds the route across all Solana DEXes
(pump.fun, PumpSwap, Raydium, Meteora...), sets priority fees and lands the transaction itself.
"""
import base64
import collections
import json
import queue
import threading
import time
from dataclasses import dataclass

import base58
import requests
import websocket
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction, VersionedTransaction

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {USDC, WSOL, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
           "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB", "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo"}  # USD1, PYUSD
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
BAD_EXTENSIONS = {"transferHook", "transferFeeConfig", "permanentDelegate", "nonTransferable", "pausableConfig"}
JUP_API = "https://api.jup.ag/swap/v2"


class SolError(Exception):
    pass


def is_sol_addr(s):
    try:
        return not str(s).startswith("0x") and len(base58.b58decode(str(s))) == 32
    except Exception:
        return False


def ata(owner, mint, program=TOKEN_PROGRAM):
    """Associated token account address of `owner` for `mint`."""
    return str(Pubkey.find_program_address([bytes(Pubkey.from_string(owner)), bytes(Pubkey.from_string(program)),
                                            bytes(Pubkey.from_string(mint))], Pubkey.from_string(ATA_PROGRAM))[0])


def new_keypair():
    kp = Keypair()
    return str(kp.pubkey()), base58.b58encode(bytes(kp)).decode()


def keypair(secret_b58):
    return Keypair.from_bytes(base58.b58decode(secret_b58))


# ================================================================== RPC
class SolRPC:
    def __init__(self, url, timeout=20, log=None):
        self.url, self.timeout, self.log = url, timeout, log
        self.s = requests.Session()
        self.s.headers.update({"Content-Type": "application/json", "User-Agent": "rhcopy/0.2"})

    def call(self, method, params):
        last = None
        for attempt in range(4):
            try:
                r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=self.timeout)
                if r.status_code == 429:
                    last = SolError("429 rate limited")
                    time.sleep(1.0 + attempt)
                    continue
                j = r.json()
                if "error" in j:
                    raise SolError(f"{method}: {j['error']}")
                return j["result"]
            except SolError:
                raise
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise SolError(f"{method} failed: {last}")

    def tx(self, sig):
        """Transactions can be legacy, v0 or v1 (newer format); ask for the newest the node knows."""
        for ver in (1, 0):
            try:
                return self.call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": ver,
                                                          "commitment": "confirmed"}])
            except SolError as e:
                if ver == 1 and "version" in str(e).lower() and "not supported" not in str(e).lower():
                    continue  # node does not know v1 at all: retry with 0
                if ver == 1 and "maxSupportedTransactionVersion" in str(e):
                    continue
                raise

    def sol_balance(self, owner):
        return self.call("getBalance", [owner, {"commitment": "confirmed"}])["value"]

    def token_accounts(self, owner, mint):
        try:
            res = self.call("getTokenAccountsByOwner", [owner, {"mint": mint},
                                                        {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
            accounts = [(a["pubkey"], a["account"]) for a in res]
        except SolError:
            # some public nodes refuse indexed queries: look at the two possible associated accounts directly
            atas = [ata(owner, mint, prog) for prog in (TOKEN_PROGRAM, TOKEN_2022)]
            vals = self.call("getMultipleAccounts", [atas, {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
            accounts = [(k, v) for k, v in zip(atas, vals) if v]
        out = []
        for pubkey, acc in accounts:
            info = acc["data"]["parsed"]["info"]
            out.append({"pubkey": pubkey, "program": acc["owner"],
                        "amount": int(info["tokenAmount"]["amount"]), "decimals": int(info["tokenAmount"]["decimals"])})
        return out

    def token_balance(self, owner, mint):
        return sum(a["amount"] for a in self.token_accounts(owner, mint))

    def mint_info(self, mint):
        v = self.call("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
        if not v:
            return None
        info = v["data"]["parsed"]["info"]
        exts = {e.get("extension") for e in info.get("extensions") or []}
        return {"program": v["owner"], "decimals": int(info.get("decimals", 0)),
                "freeze_authority": info.get("freezeAuthority"), "bad_ext": sorted(exts & BAD_EXTENSIONS)}

    def blockhash(self):
        return self.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]

    def send(self, raw_bytes):
        return self.call("sendTransaction", [base64.b64encode(raw_bytes).decode(),
                                             {"encoding": "base64", "maxRetries": 5, "preflightCommitment": "confirmed"}])

    def confirm(self, sig, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            st = self.call("getSignatureStatuses", [[sig], {"searchTransactionHistory": False}])["value"][0]
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                if st.get("err"):
                    raise SolError(f"tx {sig} failed: {st['err']}")
                return True
            time.sleep(1.0)
        raise SolError(f"tx {sig} not confirmed in {timeout}s")


# ================================================================== live watcher
class SolWatcher:
    """One WebSocket, one logsSubscribe per trader wallet. Pushes (wallet, signature) into `q`.
    Reconnects with backoff and resubscribes whenever the set of wallets changes."""

    def __init__(self, ws_url, log=None, rpc=None, poll_seconds=5.0):
        self.url, self.log, self.rpc, self.poll_seconds = ws_url, log, rpc, poll_seconds
        self.q = queue.Queue()
        self.want = frozenset()
        self.connected = False
        self.down_since = time.time()
        self._seen = collections.OrderedDict()
        self._last_sig = {}

    def _push(self, wallet, sig):
        if sig in self._seen:
            return
        self._seen[sig] = 1
        if len(self._seen) > 5000:
            self._seen.popitem(last=False)
        self.q.put((wallet, sig, time.time()))

    def set_wallets(self, wallets):
        self.want = frozenset(wallets)

    def start(self):
        if self.url:
            threading.Thread(target=self._run, daemon=True).start()
        if self.rpc:
            threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        """Fallback when the WebSocket is down for 20+ s: poll each wallet's latest signatures.
        Costs one RPC call per wallet per round, so it is only a safety net."""
        warned = False
        while True:
            time.sleep(self.poll_seconds)
            if self.connected or time.time() - self.down_since < 20 or not self.want:
                warned = False
                continue
            if not warned and self.log:
                self.log.warning("solana watcher: WebSocket down — polling %d wallets every %ss", len(self.want), self.poll_seconds)
                warned = True
            for w in sorted(self.want):
                try:
                    sigs = self.rpc.call("getSignaturesForAddress", [w, {"limit": 15, "commitment": "confirmed"}])
                except Exception:
                    continue
                last = self._last_sig.get(w)
                self._last_sig[w] = sigs[0]["signature"] if sigs else last
                if last is None:
                    continue  # first look: remember the head, do not replay history
                new = []
                for x in sigs:
                    if x["signature"] == last:
                        break
                    if x.get("err") is None:
                        new.append(x["signature"])
                for sig in reversed(new):
                    self._push(w, sig)

    def _run(self):
        backoff = 2
        while True:
            want = self.want
            if not want:
                time.sleep(2)
                continue
            ws = None
            try:
                ws = websocket.create_connection(self.url, timeout=20)
                req, subs = {}, {}
                for i, w in enumerate(sorted(want), start=1):
                    req[i] = w
                    ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                        "params": [{"mentions": [w]}, {"commitment": "confirmed"}]}))
                ws.settimeout(2)
                self.connected, backoff, last_ping = True, 2, time.time()
                self._last_sig.clear()
                if self.log:
                    self.log.info("solana watcher: subscribed to %d wallets", len(want))
                while self.want == want:
                    if time.time() - last_ping > 20:
                        ws.ping()
                        last_ping = time.time()
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    d = json.loads(msg)
                    if "id" in d and d["id"] in req and "result" in d:
                        subs[d["result"]] = req[d["id"]]
                    elif d.get("method") == "logsNotification":
                        p = d["params"]
                        v = p["result"]["value"]
                        w = subs.get(p["subscription"])
                        if w and v.get("err") is None:
                            self._push(w, v["signature"])
            except Exception as e:
                if self.log:
                    self.log.warning("solana watcher: %s — reconnecting in %ss", e, backoff)
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
            finally:
                if self.connected:
                    self.down_since = time.time()
                self.connected = False
                try:
                    if ws:
                        ws.close()
                except Exception:
                    pass


# ================================================================== trade parsing
def parse_trade(tx, wallet):
    """What happened to `wallet` in this transaction: did it sign, token balance changes (by mint),
    SOL / USDC spent or received (network fee excluded)."""
    meta, msg = tx["meta"], tx["transaction"]["message"]
    keys = msg["accountKeys"]
    pubkeys = [k["pubkey"] if isinstance(k, dict) else k for k in keys]
    signer = any(isinstance(k, dict) and k.get("pubkey") == wallet and k.get("signer") for k in keys)
    sol_delta = 0
    if wallet in pubkeys:
        i = pubkeys.index(wallet)
        sol_delta = meta["postBalances"][i] - meta["preBalances"][i]
        if i == 0:
            sol_delta += meta.get("fee", 0)  # the network fee is not part of the trade
    toks = {}
    for side in ("preTokenBalances", "postTokenBalances"):
        for b in meta.get(side) or []:
            if b.get("owner") != wallet:
                continue
            t = toks.setdefault(b["mint"], {"pre": 0, "post": 0, "dec": int(b["uiTokenAmount"]["decimals"])})
            t["pre" if side.startswith("pre") else "post"] += int(b["uiTokenAmount"]["amount"])
    wsol = toks.pop(WSOL, {"pre": 0, "post": 0})
    usdc = toks.get(USDC, {"pre": 0, "post": 0})
    return {
        "signer": signer,
        "block_time": tx.get("blockTime"),
        "slot": tx.get("slot"),
        "sol_delta": sol_delta + (wsol["post"] - wsol["pre"]),        # lamports
        "usdc_delta": usdc["post"] - usdc["pre"],                     # raw, 6 decimals
        "tokens": [{"mint": m, **t} for m, t in toks.items() if m not in STABLES and t["pre"] != t["post"]],
    }


def spent_usd(trade, sol_price):
    """USD the trader paid in SOL / USDC for the buy (0 if unknown)."""
    usd = 0.0
    if trade["usdc_delta"] < 0:
        usd += -trade["usdc_delta"] / 1e6
    if trade["sol_delta"] < 0:
        usd += -trade["sol_delta"] / 1e9 * sol_price
    return usd


# ================================================================== Jupiter
class Jupiter:
    def __init__(self, api_key=None, log=None):
        self.s = requests.Session()
        self.s.headers.update({"accept": "application/json", "User-Agent": "rhcopy/0.2"})
        if api_key:
            self.s.headers["x-api-key"] = api_key
        self.gap = 1.05 if api_key else 2.1  # free key: 1 request/s, keyless: 0.5 request/s
        self.lock = threading.Lock()
        self.last = 0.0
        self.last_error = ""
        self.log = log

    def _throttle(self):
        with self.lock:
            wait = self.gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()

    def order(self, input_mint, output_mint, amount, taker=None):
        params = {"inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount))}
        if taker:
            params["taker"] = taker
        for attempt in range(3):
            self._throttle()
            try:
                r = self.s.get(f"{JUP_API}/order", params=params, timeout=15)
            except Exception as e:
                self.last_error = f"jupiter: {e}"
                continue
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            try:
                j = r.json()
            except ValueError:
                j = {}
            if r.status_code != 200 or "outAmount" not in j:
                self.last_error = f"jupiter {r.status_code}: {str(j)[:200]}"
                return None
            return j
        self.last_error = self.last_error or "jupiter rate limited"
        return None

    def execute(self, signed_b64, request_id):
        r = self.s.post(f"{JUP_API}/execute", json={"signedTransaction": signed_b64, "requestId": request_id}, timeout=90)
        try:
            return r.json()
        except ValueError:
            return {"status": "Failed", "error": f"HTTP {r.status_code}"}


# ================================================================== executor
@dataclass
class SolFill:
    tokens_raw: int
    usd: float
    gas_usd: float
    tx: str
    quote_impact_pct: float = 0.0


def _fees_lamports(order, taker):
    total = 0
    for k in ("signatureFee", "prioritizationFee", "rentFee"):
        payer = order.get(f"{k}Payer")
        if payer in (None, taker):
            total += int(order.get(f"{k}Lamports") or 0)
    return total


class SolExecutor:
    """Paper mode: real Jupiter quotes, nothing is signed. Live: /order with our wallet as taker,
    sign, /execute (Jupiter lands it)."""

    def __init__(self, rpc: SolRPC, jup: Jupiter, live, secret_b58=None, sol_price=lambda: 0.0,
                 est_fee_sol=0.0002, log=None):
        self.rpc, self.jup, self.live, self.log = rpc, jup, bool(live), log
        self.kp = keypair(secret_b58) if secret_b58 else None
        if self.live and not self.kp:
            raise SolError("live Solana trading needs a wallet")
        self.addr = str(self.kp.pubkey()) if self.kp else None
        self.sol_price, self.est_fee_sol = sol_price, est_fee_sol

    def buy(self, mint, usd):
        return self._swap(USDC, mint, int(round(usd * 1e6)), buying=True)

    def sell(self, mint, tokens_raw):
        return self._swap(mint, USDC, int(tokens_raw), buying=False)

    def _swap(self, inp, out, amount, buying):
        if not self.live:
            q = self.jup.order(inp, out, amount)
            if not q or int(q.get("outAmount") or 0) <= 0:
                raise SolError(self.jup.last_error or "no route")
            gas = self.est_fee_sol * self.sol_price()
            impact = float(q.get("priceImpact") or 0)
            if buying:
                return SolFill(int(q["outAmount"]), amount / 1e6, gas, "paper", impact)
            return SolFill(amount, int(q["outAmount"]) / 1e6, gas, "paper", impact)
        q = self.jup.order(inp, out, amount, taker=self.addr)
        if not q:
            raise SolError(self.jup.last_error or "no route")
        if not q.get("transaction"):
            raise SolError(q.get("errorMessage") or q.get("error") or "Jupiter could not build the swap")
        res = self.jup.execute(self._sign(q["transaction"]), q["requestId"])
        if res.get("status") != "Success":
            raise SolError(f"swap failed: code {res.get('code')} {res.get('error') or ''}".strip())
        gas = _fees_lamports(q, self.addr) / 1e9 * self.sol_price()
        impact = float(q.get("priceImpact") or 0)
        if buying:
            return SolFill(int(res["totalOutputAmount"]), int(res["totalInputAmount"]) / 1e6, gas, res["signature"], impact)
        return SolFill(int(res["totalInputAmount"]), int(res["totalOutputAmount"]) / 1e6, gas, res["signature"], impact)

    def _sign(self, tx_b64):
        """Sign only our slot: RFQ routes carry a market maker signature that Jupiter adds on /execute."""
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        msg = tx.message
        keys = list(msg.account_keys)
        i = keys.index(self.kp.pubkey())
        sigs = list(tx.signatures)
        sigs[i] = self.kp.sign_message(to_bytes_versioned(msg))
        return base64.b64encode(bytes(VersionedTransaction.populate(msg, sigs))).decode()

    # ---------- wallet operations (live wallet required) ----------
    def balances(self):
        return {"usdc": self.rpc.token_balance(self.addr, USDC) / 1e6, "sol": self.rpc.sol_balance(self.addr) / 1e9}

    def _send_ixs(self, ixs):
        bh = Hash.from_string(self.rpc.blockhash())
        tx = Transaction.new_signed_with_payer(ixs, self.kp.pubkey(), [self.kp], bh)
        sig = self.rpc.send(bytes(tx))
        self.rpc.confirm(sig)
        return sig

    def close_empty_accounts(self, mint):
        """Return the ~0.002 SOL deposit of emptied token accounts of `mint`."""
        owner = self.kp.pubkey()
        ixs = [Instruction(Pubkey.from_string(a["program"]), bytes([9]),
                           [AccountMeta(Pubkey.from_string(a["pubkey"]), False, True),
                            AccountMeta(owner, False, True), AccountMeta(owner, True, False)])
               for a in self.rpc.token_accounts(self.addr, mint) if a["amount"] == 0]
        return self._send_ixs(ixs) if ixs else None

    def send_sol(self, to, lamports):
        ix = transfer(TransferParams(from_pubkey=self.kp.pubkey(), to_pubkey=Pubkey.from_string(to), lamports=int(lamports)))
        return self._send_ixs([ix])

    def send_token(self, mint, to, amount_raw):
        """SPL transfer to the recipient's associated account, creating it if needed (the payer is us)."""
        accs = [a for a in self.rpc.token_accounts(self.addr, mint) if a["amount"] > 0]
        if not accs:
            raise SolError("no balance")
        src = max(accs, key=lambda a: a["amount"])
        prog, mint_pk, owner = Pubkey.from_string(src["program"]), Pubkey.from_string(mint), self.kp.pubkey()
        dest_owner = Pubkey.from_string(to)
        ata_prog = Pubkey.from_string(ATA_PROGRAM)
        dest_ata = Pubkey.find_program_address([bytes(dest_owner), bytes(prog), bytes(mint_pk)], ata_prog)[0]
        create = Instruction(ata_prog, bytes([1]), [
            AccountMeta(owner, True, True), AccountMeta(dest_ata, False, True), AccountMeta(dest_owner, False, False),
            AccountMeta(mint_pk, False, False), AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM), False, False),
            AccountMeta(prog, False, False)])
        data = bytes([12]) + int(amount_raw).to_bytes(8, "little") + bytes([src["decimals"]])
        xfer = Instruction(prog, data, [
            AccountMeta(Pubkey.from_string(src["pubkey"]), False, True), AccountMeta(mint_pk, False, False),
            AccountMeta(dest_ata, False, True), AccountMeta(owner, True, False)])
        return self._send_ixs([create, xfer])
