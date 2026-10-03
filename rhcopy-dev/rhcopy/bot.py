"""The copy bot, several users in one process: one watcher over everyone's traders -> shared facts
about each fill -> per-user gates, sizing and buys -> per-user exits. Each user has their own
traders, settings, positions, paper bankroll and (for live) their own wallet."""
import copy
import json
import secrets
import time
from dataclasses import dataclass

from eth_account import Account

from . import strategy
from .chain import SWAP_TOPICS, TRANSFER, Chain, RPCError, pad_addr, topic_addr
from .db import DB
from .executor import ExecError, Executor
from .fmt import NOISE, SKIP_RU, big, exit_reason, skip_detail, usd
from .market import Market
from .relay import APPROVAL_PROXY, SOLANA_CHAIN_ID, Relay
from .sources import MadeOnSol
from .sol import USDC as SOL_USDC, WSOL, Jupiter, SolError, SolExecutor, SolRPC, SolWatcher, is_sol_addr, keypair, parse_trade, spent_usd
from .telegram import Telegram
from .ui import REPLY_KB, UI, B

ZERO = "0x0000000000000000000000000000000000000000"
EDITABLE = ("sizing", "gates", "exits", "execution", "chains")
CHAIN_NAME = {"rh": "Robinhood", "sol": "Solana", "eth": "Ethereum", "bsc": "BNB Chain", "base": "Base"}
DEX_PATH = {"rh": "robinhood", "sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base"}
CHAIN_KEY = {"rh": "robinhood", "sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base"}
ETH_USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
ETH_WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
ETH_STABLES = {ETH_USDC, ETH_WETH, "0xdac17f958d2ee523a2206206994597c13d831ec7", "0x6b175474e89094c44da98b954eedeac495271d0f",
               "0x6c3ea9036406852006290770bedfcaba0e23a0e8"}  # USDT, DAI, PYUSD
# BNB Chain: the bot trades with USDT, which has 18 decimals here (not 6)
BSC_USDT = "0x55d398326f99059ff775485246999027b3197955"
BSC_WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
BSC_STABLES = {BSC_USDT, BSC_WBNB, "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",   # USDC (18 dec on BSC)
               "0xe9e7cea3dedca5984780bafc599bd69add087d56"}                       # BUSD
# Base: the bot trades with USDC (6 decimals)
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_STABLES = {BASE_USDC, BASE_WETH, "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC
                "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"}                        # DAI


@dataclass
class EvmNet:
    """One EVM network the bot watches and trades on (Robinhood Chain, Ethereum, BNB Chain, Base)."""
    key: str
    chain_id: int
    chain: Chain
    market: Market
    usd: str            # the stablecoin the bot trades with
    ignore: set
    last_key: str       # where the last scanned block is stored
    chunk: int
    max_catch: int
    poll: float
    min_gas: float      # live: below this native balance no new buys
    max_gas_pct: float  # floor for the gas gate on this network
    usd_decimals: int = 6     # stablecoin decimals (BNB Chain USDT = 18)
    coin: str = "ETH"         # native gas coin (BNB on BNB Chain)
    usd_name: str = "USDC"    # stablecoin ticker for the UI
    explorer: str = ""        # block explorer base url
    last_scan: float = 0.0


class Skip(Exception):
    def __init__(self, reason, **details):
        super().__init__(reason)
        self.reason, self.details = reason, details


def valid_addr(a):
    """A trader address the bot can watch: an EVM wallet (not the zero placeholder) or a Solana wallet."""
    a = str(a or "")
    if a.lower().startswith("0x"):
        return len(a) == 42 and a.lower() != ZERO
    return is_sol_addr(a)


class CopyBot:
    def __init__(self, cfg, db: DB, env, log):
        # config.yaml from an older version has no networks / Solana sections: fill in the defaults
        cfg.setdefault("chains", {})
        cfg["chains"].setdefault("robinhood", True)
        cfg["chains"].setdefault("solana", True)
        cfg.setdefault("solana", {})
        for k, v in {"rpc": "https://solana-rpc.publicnode.com", "ws": "wss://api.mainnet-beta.solana.com",
                     "poll_seconds": 5, "min_sol": 0.02, "est_fee_sol": 0.0002, "round_trip_check": False,
                     "ws_wallets_per_conn": 50}.items():  # one WebSocket per this many watched wallets
            cfg["solana"].setdefault(k, v)
        cfg.setdefault("sources", {}).setdefault("max_traders_per_user", 200)
        cfg["chains"].setdefault("ethereum", True)
        cfg.setdefault("ethereum", {})
        for k, v in {"rpc": ["https://eth.drpc.org", "https://rpc.mevblocker.io", "https://eth-mainnet.public.blastapi.io"],
                     "poll_seconds": 6, "log_chunk_blocks": 40, "max_catchup_blocks": 25,
                     "min_gas_eth": 0.002, "max_gas_pct": 3.0}.items():
            cfg["ethereum"].setdefault(k, v)
        cfg["chains"].setdefault("bsc", True)
        cfg.setdefault("bsc", {})
        for k, v in {"rpc": ["https://rpc-bsc.48.club", "https://56.rpc.thirdweb.com", "https://1rpc.io/bnb"],
                     "poll_seconds": 2, "log_chunk_blocks": 45, "max_catchup_blocks": 600,
                     "min_gas_eth": 0.003, "max_gas_pct": 1.5}.items():  # ~0.45 s blocks; 1rpc caps getLogs at 50 blocks
            cfg["bsc"].setdefault(k, v)
        cfg["chains"].setdefault("base", True)
        cfg.setdefault("base", {})
        for k, v in {"rpc": ["https://mainnet.base.org", "https://8453.rpc.thirdweb.com",
                             "https://gateway.tenderly.co/public/base"],
                     "poll_seconds": 3, "log_chunk_blocks": 500, "max_catchup_blocks": 150,
                     "min_gas_eth": 0.0005, "max_gas_pct": 1.5}.items():  # ~2 s blocks
            cfg["base"].setdefault(k, v)
        self.base_cfg, self.db, self.env, self.log = cfg, db, env, log
        rpcs = [env.get("RPC_URL") or cfg["rpc"], cfg.get("fallback_rpc")]
        self.chain = Chain(rpcs, log=log)
        self.relay = Relay(api_key=env.get("RELAY_API_KEY"), log=log)
        self.market = Market()
        self.usdg, self.weth = cfg["usdg"].lower(), cfg["weth"].lower()
        self.ignore = {self.usdg, self.weth, *[t.lower() for t in cfg.get("ignore_tokens", [])]}
        db.migrate(cfg, env)
        for w in cfg.get("wallets") or []:  # config.yaml traders belong to the admin (user #1)
            a = str(w.get("address") or "").lower()
            if not valid_addr(a):
                log.warning("config.yaml: trader %s has a placeholder/invalid address %r - skipped", w.get("label"), a)
                continue
            if not db.trader(1, a):
                db.add_trader(1, a, w.get("label") or a[:8], w.get("ticket_usd"))
            if w.get("solana") and not db.pairs(a):
                db.set_pair(a, w["solana"])
        for u in db.users(active_only=False):
            for t in db.traders(u["id"]):
                if not valid_addr(t["address"]):
                    db.set_trader(u["id"], t["address"], active=0)
        self._cfg, self._exes = {}, {}
        self._eth = self._bnb = (0.0, 0.0)
        self._last_prices = self._last_beat = 0.0
        self._gas_alert, self._hinted = {}, {}
        self._rr = 0
        tg_token = env.get("TELEGRAM_BOT_TOKEN")
        self.tg = Telegram(tg_token, log) if tg_token and cfg.get("telegram", {}).get("enabled", True) else None
        self.ui = UI(self) if self.tg else None
        self.admin_code = secrets.token_hex(3) if self.tg and not db.user(1)["chat_id"] else None
        # ---- EVM networks: Robinhood Chain always; Ethereum / BNB Chain / Base unless switched off
        self.nets = {"rh": EvmNet("rh", 4663, self.chain, self.market, self.usdg, self.ignore, "last_block",
                                  int(cfg.get("log_chunk_blocks", 2000)), int(cfg.get("max_catchup_blocks", 3000)),
                                  float(cfg.get("poll_seconds", 1.0)),
                                  float(cfg["execution"].get("min_gas_eth", 0.0005)), 0.0,
                                  usd_decimals=6, coin="ETH", usd_name="USDG", explorer="https://robin.etherscan.io")}
        extra = {t.lower() for t in cfg.get("ignore_tokens", [])}

        def evm_net(key, section, chain_id, rpc_env, usd, stables, last_key, usd_decimals, coin, usd_name, explorer):
            sec = cfg[section]
            rpcs = sec["rpc"] if isinstance(sec["rpc"], list) else [sec["rpc"]]
            chain = Chain(([env.get(rpc_env)] if rpc_env else []) + rpcs, log=log)
            return EvmNet(key, chain_id, chain, Market(DEX_PATH[key]), usd, stables | extra, last_key,
                          int(sec["log_chunk_blocks"]), int(sec["max_catchup_blocks"]), float(sec["poll_seconds"]),
                          float(sec["min_gas_eth"]), float(sec["max_gas_pct"]),
                          usd_decimals=usd_decimals, coin=coin, usd_name=usd_name, explorer=explorer)

        for key, section, cfgkey, cid, rpc_env, usd, stables, last_key, dec, coin, name, expl in (
                ("eth", "ethereum", "ethereum", 1, "ETH_RPC_URL", ETH_USDC, ETH_STABLES, "last_block_eth", 6, "ETH", "USDC", "https://etherscan.io"),
                ("bsc", "bsc", "bsc", 56, "BSC_RPC_URL", BSC_USDT, BSC_STABLES, "last_block_bsc", 18, "BNB", "USDT", "https://bscscan.com"),
                ("base", "base", "base", 8453, "BASE_RPC_URL", BASE_USDC, BASE_STABLES, "last_block_base", 6, "ETH", "USDC", "https://basescan.org")):
            if cfg["chains"].get(cfgkey, True):
                self.nets[key] = evm_net(key, section, cid, rpc_env, usd, stables, last_key, dec, coin, name, expl)
        # ---- Solana
        scfg = cfg.get("solana") or {}
        helius = env.get("HELIUS_API_KEY")
        rpc = env.get("SOLANA_RPC_URL") or (f"https://mainnet.helius-rpc.com/?api-key={helius}" if helius else scfg.get("rpc"))
        ws = env.get("SOLANA_WS_URL") or (f"wss://mainnet.helius-rpc.com/?api-key={helius}" if helius else scfg.get("ws"))
        self.sol_enabled = bool((cfg.get("chains") or {}).get("solana", True)) and bool(rpc)
        self.sol_rpc = SolRPC(rpc, log=log) if self.sol_enabled else None
        self.sol_watch = SolWatcher(ws, log, rpc=self.sol_rpc, poll_seconds=float(scfg.get("poll_seconds", 5)),
                                    per_conn=int(scfg.get("ws_wallets_per_conn", 50))) if self.sol_enabled else None
        self.jup = Jupiter(env.get("JUPITER_API_KEY"), log)
        self.market_sol = Market("solana")
        self.madeonsol = MadeOnSol(env.get("MADEONSOL_API_KEY"), log)
        self._sol_exes, self._sol_px = {}, (0.0, 0.0)

    # ------------------------------------------------------------------ per-user config
    def ucfg(self, u):
        ov = u.get("overrides") or "{}"
        hit = self._cfg.get(u["id"])
        if hit and hit[0] == ov:
            return hit[1]
        cfg = copy.deepcopy(self.base_cfg)
        for key, val in json.loads(ov).items():
            node = cfg
            *path, last = key.split(".")
            for p in path:
                node = node.setdefault(p, {})
            node[last] = val
        self._cfg[u["id"]] = (ov, cfg)
        return cfg

    def set_override(self, u, key, raw):
        if key.split(".")[0] not in EDITABLE:
            return f"менять можно только {', '.join(EDITABLE)}"
        node = self.ucfg(u)
        *path, last = key.split(".")
        for p in path:
            if p not in node or not isinstance(node[p], dict):
                return f"нет такого ключа {key}"
            node = node[p]
        if last not in node:
            return f"нет такого ключа {key}"
        try:
            val = json.loads(raw)
        except json.JSONDecodeError:
            val = raw
        ov = json.loads(u.get("overrides") or "{}")
        ov[key] = val
        u["overrides"] = json.dumps(ov)
        self.db.update_user(u["id"], overrides=u["overrides"])
        return f"{key} = {json.dumps(val, ensure_ascii=False)}"

    def get_key(self, u, key):
        node = self.ucfg(u)
        for p in key.split("."):
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    # ------------------------------------------------------------------ wallets & money
    def user_key(self, u):
        return u.get("private_key") or (self.env.get("PRIVATE_KEY") if u["id"] == 1 else None) or None

    def wallet_address(self, u):
        k = self.user_key(u)
        return Account.from_key(k).address if k else None

    def is_live(self, u):
        return self.live_on(u, "rh")

    def exe(self, u, live, chain="rh"):
        net = self.nets[chain]
        key = self.user_key(u)
        live = bool(live and key)
        sig = (live, key, u.get("overrides"))
        hit = self._exes.get((u["id"], chain, live))
        if hit and hit[0] == sig:
            return hit[1]
        cfg = {"live": live, "usdg": net.usd, "usd_decimals": net.usd_decimals, "execution": self.ucfg(u)["execution"]}
        price_fn = self.bnb_price if net.coin == "BNB" else self.eth_price
        ex = Executor(net.chain, self.relay, cfg, key if live else None, price_fn, self.log,
                      chain_id=net.chain_id, approval_proxy=APPROVAL_PROXY)
        self._exes[(u["id"], chain, live)] = (sig, ex)
        return ex

    def eth_price(self):
        ts, p = self._eth
        if time.time() - ts > 120 or p <= 0:
            info = self.market.info(self.weth, fresh=True)
            if info and info["price_usd"] > 0:
                self._eth = (time.time(), info["price_usd"])
        return self._eth[1]

    def bnb_price(self):
        ts, p = self._bnb
        if time.time() - ts > 120 or p <= 0:
            net = self.nets.get("bsc")
            info = net.market.info(BSC_WBNB, fresh=True) if net else None
            if info and info["price_usd"] > 0:
                self._bnb = (time.time(), info["price_usd"])
        return self._bnb[1]

    # ---- Solana wallet of a user
    def sol_key(self, u):
        return u.get("sol_key") or (self.env.get("SOLANA_PRIVATE_KEY") if u["id"] == 1 else None) or None

    def sol_address(self, u):
        k = self.sol_key(u)
        return str(keypair(k).pubkey()) if k else None

    def sol_price(self):
        ts, p = self._sol_px
        if time.time() - ts > 60 or p <= 0:
            px = self.market_sol.prices([WSOL]).get(WSOL)
            if px:
                self._sol_px = (time.time(), px)
        return self._sol_px[1]

    def sol_exe(self, u, live):
        key = self.sol_key(u)
        live = bool(live and key)
        sig = (live, key)
        hit = self._sol_exes.get((u["id"], live))
        if hit and hit[0] == sig:
            return hit[1]
        ex = SolExecutor(self.sol_rpc, self.jup, live, key if live else None, self.sol_price,
                         float((self.base_cfg.get("solana") or {}).get("est_fee_sol", 0.0002)), self.log)
        self._sol_exes[(u["id"], live)] = (sig, ex)
        return ex

    def live_chains(self, u):
        """The networks this user has switched to LIVE (JSON list `users.live_chains`). Legacy rows
        not yet backfilled fall back to the old per-chain flags."""
        lc = u.get("live_chains")
        if lc is not None:
            return set(json.loads(lc))
        return {c for c, v in (("rh", u.get("live")), ("sol", u.get("sol_live")), ("eth", u.get("eth_live"))) if v}

    def set_live(self, u, chain, on):
        chains = self.live_chains(u)
        chains.add(chain) if on else chains.discard(chain)
        u["live_chains"] = json.dumps(sorted(chains))
        self.db.update_user(u["id"], live_chains=u["live_chains"])

    def live_on(self, u, chain):
        if chain not in self.live_chains(u):
            return False
        return bool(self.sol_key(u)) if chain == "sol" else bool(self.user_key(u))

    def cash(self, u, chain="rh"):
        if self.live_on(u, chain):
            if chain == "sol":
                return self.sol_rpc.token_balance(self.sol_address(u), SOL_USDC) / 1e6
            net = self.nets[chain]
            return net.chain.erc20_balance(net.usd, self.wallet_address(u)) / 10 ** net.usd_decimals
        return float(u.get("paper_cash") or 0)

    def open_value(self, u, chain="rh"):
        """Live: value of live positions on that chain. Paper: all paper positions (one paper bank)."""
        live = self.live_on(u, chain)

        def counts(p):
            return (not p["paper"] and (p.get("chain") or "rh") == chain) if live else bool(p["paper"])
        return sum(p["tokens_left"] / 10 ** p["decimals"] * (p["last_price"] or 0)
                   for p in self.db.positions(u["id"]) if counts(p))

    def equity(self, u, chain="rh"):
        return self.cash(u, chain) + self.open_value(u, chain)

    def add_paper_cash(self, uid, delta):
        u = self.db.user(uid)
        self.db.update_user(uid, paper_cash=float(u["paper_cash"] or 0) + delta)

    # ------------------------------------------------------------------ notify
    def notify(self, u, text, kb=None):
        self.log.info("[%s] %s", u.get("name"), text.replace("\n", " | "))
        if self.tg and u.get("chat_id") and u.get("active"):
            self.tg.send(u["chat_id"], text, kb=kb)

    def notify_admin(self, text, kb=None):
        self.notify(self.db.user(1), text, kb)

    # ------------------------------------------------------------------ main loop
    def run(self):
        for net in list(self.nets.values()):
            try:
                cid = net.chain.chain_id()
            except RPCError as e:
                if net.key == "rh":
                    raise
                self.log.warning("%s RPC unreachable (%s) — this network is off until restart", CHAIN_NAME[net.key], e)
                self.nets.pop(net.key)
                continue
            if cid != net.chain_id:
                raise SystemExit(f"{CHAIN_NAME[net.key]} RPC is chain {cid}, expected {net.chain_id}")
        users = self.db.users()
        self.log.info("rhcopy started · users=%d · traders watched=%d", len(users), len(self.db.watched()))
        for a in self.db.watched():
            if not self.db.pairs(a):
                self.log.info(self.learn_pair(a))
        if self.sol_watch:
            self.sol_watch.set_wallets(self.db.sol_watch_map())
            self.sol_watch.start()
            self.log.info("solana: watching %d wallets", len(self.db.sol_watch_map()))
        if self.tg:
            self.tg.start()
            admin = self.db.user(1)
            if admin["chat_id"]:
                self.tg.send(admin["chat_id"], f"🤖 rhcopy запущен · пользователей: {len(users)}", reply_kb=REPLY_KB)
            else:
                self.log.warning("Telegram not paired: send  /start %s  to your bot", self.admin_code)
        while True:
            t0 = time.time()
            try:
                self.handle_inbox()
                self.scan()
                self.scan_sol()
                self.recheck_provenance()
                if time.time() - self._last_prices >= float(self.base_cfg["exits"].get("price_check_seconds", 10)):
                    self._last_prices = time.time()
                    self.update_positions()
                self.heartbeat()
            except RPCError as e:
                self.log.warning("rpc: %s", e)
                time.sleep(3)
            except Exception as e:  # keep running; exits must keep working
                self.log.exception("loop error: %s", e)
                time.sleep(3)
            time.sleep(max(0.0, float(self.base_cfg.get("poll_seconds", 1.0)) - (time.time() - t0)))

    def heartbeat(self):
        if time.time() - self._last_beat < 120:
            return
        self._last_beat = time.time()
        self.log.info("[beat] rh=%s eth=%s users=%d open=%d", self.db.get("last_block"), self.db.get("last_block_eth"),
                      len(self.db.users()), len(self.db.positions()))

    # ------------------------------------------------------------------ telegram: who is talking
    def handle_inbox(self):
        if not self.tg:
            return
        while not self.tg.inbox.empty():
            it = self.tg.inbox.get()
            u = self.db.user_by_chat(it["chat"])
            try:
                if not u or not u["active"]:
                    self.on_stranger(it)
                elif it["kind"] == "cb":
                    self.ui.on_callback(u, it)
                else:
                    self.ui.on_text(u, it["text"])
            except Exception as e:
                self.log.exception("telegram update failed: %s", e)
                self.tg.send(it["chat"], f"ошибка: {e}")

    def on_stranger(self, it):
        if it["kind"] == "cb":
            return self.tg.answer(it["cb_id"])
        text, chat = it["text"], it["chat"]
        if text.startswith("/start "):
            code = text.split(maxsplit=1)[1].strip()
            if self.admin_code and code == self.admin_code:
                self.db.update_user(1, chat_id=chat, name=it.get("name") or "admin")
                self.admin_code = None
                return self.ui.on_text(self.db.user(1), "/start")
            invites = self.db.get("invites") or {}
            if code in invites and invites[code] > time.time():
                invites.pop(code)
                self.db.put("invites", invites)
                old = self.db.user_by_chat(chat)
                if old:  # a removed user coming back
                    self.db.update_user(old["id"], active=1)
                    uid = old["id"]
                else:
                    uid = self.db.add_user(chat, it.get("name") or "user", float(self.base_cfg.get("paper_cash_usd", 500)))
                self.notify_admin(f"👋 К боту присоединился {it.get('name')}")
                return self.ui.on_text(self.db.user(uid), "/start")
        if time.time() - self._hinted.get(chat, 0) > 600:
            self._hinted[chat] = time.time()
            self.tg.send(chat, "Это приватный бот. Чтобы войти, нужен код приглашения от владельца: /start КОД")

    def new_invite(self):
        invites = {k: v for k, v in (self.db.get("invites") or {}).items() if v > time.time()}
        code = secrets.token_hex(4)
        invites[code] = time.time() + 24 * 3600
        self.db.put("invites", invites)
        return code

    # ------------------------------------------------------------------ detection (shared by all users)
    def scan(self):
        watched = self.db.watched()
        if not watched:
            return
        for net in list(self.nets.values()):
            if time.time() - net.last_scan < net.poll:
                continue
            net.last_scan = time.time()
            try:
                self.scan_net(net, watched)
            except RPCError as e:
                if net.key == "rh":
                    raise
                self.log.warning("%s: %s", CHAIN_NAME[net.key], e)

    def scan_net(self, net, watched):
        head = net.chain.head()
        last = self.db.get(net.last_key)
        if last is None or head - last > net.max_catch:
            last = head - (1 if last is None else net.max_catch)
        topics = [pad_addr(a) for a in watched]
        watch_set = set(watched)
        frm = last + 1
        while frm <= head:
            to = min(head, frm + net.chunk - 1)
            flt = {"fromBlock": hex(frm), "toBlock": hex(to)}
            inc_all, out_all = [], []
            for i in range(0, len(topics), 100):  # RPCs cap the topics array: watch in batches of 100 wallets
                chunk = topics[i:i + 100]
                inc, out = net.chain.batch([("eth_getLogs", [{**flt, "topics": [TRANSFER, None, chunk]}]),
                                            ("eth_getLogs", [{**flt, "topics": [TRANSFER, chunk]}])])
                for r in (inc, out):
                    if isinstance(r, Exception):
                        raise r
                inc_all += inc
                out_all += out
            self.on_outgoing(out_all, net)
            self.on_incoming(inc_all, watch_set, net)
            self.db.put(net.last_key, to)
            frm = to + 1

    def on_outgoing(self, logs, net=None):
        net = net or self.nets["rh"]
        for lg in logs:
            if len(lg["topics"]) != 3:
                continue  # ERC-721 and other non-fungible Transfer events
            token, src, dst = lg["address"].lower(), topic_addr(lg["topics"][1]), topic_addr(lg["topics"][2])
            ps = [p for p in self.db.open_for_token(token) if p["wallet"] == src and not p["state"].get("origin_exit")
                  and (p.get("chain") or "rh") == net.key]
            if not ps or not (dst == ZERO or net.chain.is_contract(dst)):
                continue
            for p in ps:
                p["state"]["origin_exit"] = True
                self.db.update_position(p["id"], state=json.dumps(p["state"]))
                self.notify(self.db.user(p["user_id"]), f"↘ {p['label']} начал продавать {p['symbol']} ({CHAIN_NAME[net.key]}) — выхожу следом",
                            kb=[[B("📄 Позиция", f"p:{p['id']}")]])

    def on_incoming(self, logs, watched, net=None):
        net = net or self.nets["rh"]
        seen = set()
        for lg in logs:
            if len(lg["topics"]) != 3 or len(lg.get("data") or "0x") <= 2:
                continue  # ERC-721 / odd Transfer events carry no ERC-20 amount
            token = lg["address"].lower()
            wallet, sender = topic_addr(lg["topics"][2]), topic_addr(lg["topics"][1])
            key = (lg["transactionHash"], wallet, token)
            if key in seen or token in net.ignore or sender == ZERO or wallet not in watched:
                continue
            seen.add(key)
            try:
                if not net.chain.is_contract(sender):
                    continue  # wallet-to-wallet transfer, not a fill
                blk = int(lg["blockNumber"], 16)
                try:
                    prev = net.chain.erc20_balance(token, wallet, hex(blk - 1))
                except RPCError:
                    prev = 0
                if prev > 0:
                    # adding to a bag the trader already holds: not an entry. Report it (don't buy).
                    self.on_add_buy(wallet, token, int(lg["data"][:66], 16), blk, lg["transactionHash"], net)
                    continue
                self.on_signal(wallet, token, int(lg["data"][:66], 16), blk, lg["transactionHash"], net)
            except RPCError:
                raise  # network trouble: retry the whole block range
            except Exception as e:  # one broken token must not stall the watcher
                self.log.exception("signal %s in %s failed: %s", token, lg["transactionHash"], e)

    def on_add_buy(self, wallet, token, amount_raw, blk, txh, net):
        """A trader bought more of a coin he already holds (EVM). Not an entry: report it as
        not_first_buy to followers who don't hold it, and never buy on an add."""
        followers = self.db.followers(wallet)
        if not followers:
            return
        info = net.market.info(token)  # cached: do not fetch fresh in the scan loop
        price = (info or {}).get("price_usd") or 0
        add_usd = amount_raw / 10 ** net.chain.decimals(token) * price if price else None
        self.report_not_first_buy(followers, token, net.chain.symbol(token), add_usd, blk, txh, net.key, info)

    def report_not_first_buy(self, followers, token, symbol, add_usd, blk, txh, chain, info):
        """Record a not_first_buy skip for each follower who is not already holding the coin
        (if they hold it, an add is ordinary and we say nothing)."""
        seen = set()
        for t in followers:
            uid = t["user_id"]
            if uid in seen or self.db.holding(uid, token):
                continue
            seen.add(uid)
            s = Skip("not_first_buy", add_usd=round(add_usd, 2) if add_usd else None, chain=chain)
            self.log_skip(self.db.user(uid), t, token, symbol, txh, blk, s, {"chain": chain, "info": info})

    # ------------------------------------------------------------------ Solana detection
    def scan_sol(self):
        if not self.sol_watch:
            return
        wmap = self.db.sol_watch_map()
        self.sol_watch.set_wallets(wmap)
        for _ in range(20):
            if self.sol_watch.q.empty():
                break
            wallet, sig, _seen = self.sol_watch.q.get()
            try:
                self.on_sol_tx(wallet, sig, wmap.get(wallet, set()))
            except SolError as e:
                self.log.warning("solana tx %s: %s", sig[:12], e)
            except Exception as e:  # one broken transaction must not stall the watcher
                self.log.exception("solana tx %s failed: %s", sig[:12], e)

    def on_sol_tx(self, wallet, sig, traders):
        tx = None
        for _ in range(5):  # a just-confirmed transaction can take a moment to be served
            tx = self.sol_rpc.tx(sig)
            if tx:
                break
            time.sleep(0.4)
        if not tx or (tx.get("meta") or {}).get("err"):
            return
        tr = parse_trade(tx, wallet)
        for ev in tr["tokens"]:
            if ev["post"] < ev["pre"]:
                if tr["signer"]:
                    self.on_sol_exit(ev["mint"], traders)
            elif ev["pre"] == 0:  # a first buy, not an add to an existing bag
                for trader in traders:
                    self.on_signal_sol(trader, wallet, ev, tr, sig)
            elif tr["signer"]:  # pre > 0 and post > pre: the trader added to a bag he already holds
                self.on_add_buy_sol(wallet, ev, tr, sig, traders)

    def on_sol_exit(self, mint, traders):
        for p in self.db.open_for_token(mint):
            if p.get("chain") == "sol" and p["wallet"] in traders and not p["state"].get("origin_exit"):
                p["state"]["origin_exit"] = True
                self.db.update_position(p["id"], state=json.dumps(p["state"]))
                self.notify(self.db.user(p["user_id"]), f"↘ {p['label']} начал продавать {p['symbol']} (Solana) — выхожу следом",
                            kb=[[B("📄 Позиция", f"p:{p['id']}")]])

    def on_add_buy_sol(self, wallet, ev, tr, sig, traders):
        """The trader added to a Solana bag he already holds. Report not_first_buy, never buy."""
        followers = [t for trader in traders for t in self.db.followers(trader)]
        if not followers:
            return
        mint = ev["mint"]
        info = self.market_sol.info(mint)  # cached
        paid = spent_usd(tr, self.sol_price())
        add_usd = paid if paid > 0 else ((ev["post"] - ev["pre"]) / 10 ** ev["dec"] * info["price_usd"]
                                         if info and info.get("price_usd") else None)
        symbol = (info or {}).get("symbol") or mint[:6]
        self.report_not_first_buy(followers, mint, symbol, add_usd, tr.get("slot") or 0, sig, "sol", info)

    def on_signal_sol(self, trader, wallet, ev, tr, sig):
        followers = self.db.followers(trader)
        if not followers:
            return
        mint = ev["mint"]
        users = {t["user_id"]: self.db.user(t["user_id"]) for t in followers}
        info = self.market_sol.info(mint, fresh=True)
        symbol = (info or {}).get("symbol") or mint[:6]
        try:
            f = self.facts_sol(wallet, mint, ev, tr, info)
        except Skip as s:
            for t in followers:
                self.log_skip(users[t["user_id"]], t, mint, symbol, sig, tr.get("slot") or 0, s, {"chain": "sol", "info": info})
            return
        self._rr += 1
        k = self._rr % len(followers)
        for t in followers[k:] + followers[:k]:
            u = users[t["user_id"]]
            try:
                ticket, d = self.judge(u, t, mint, f)
            except Skip as s:
                self.log_skip(u, t, mint, symbol, sig, tr.get("slot") or 0, s, f)
                continue
            self.buy(u, t, mint, symbol, ticket, d, tr.get("slot") or 0, sig)

    def facts_sol(self, wallet, mint, ev, tr, info):
        """What a Solana fill looks like regardless of whose bot it is."""
        if not tr["signer"]:
            raise Skip("not_signed")  # tokens pushed into the trader's wallet by someone else
        f = {"chain": "sol", "age_s": round(time.time() - (tr["block_time"] or time.time()), 1), "own": True,
             "paid_eth": False, "stock": False, "rec": None, "pair": set(), "tx_from": wallet, "tx_to": "", "info": info,
             "dec": ev["dec"], "amount": (ev["post"] - ev["pre"]) / 10 ** ev["dec"]}
        mi = self.sol_rpc.mint_info(mint)
        if mi and mi["freeze_authority"]:
            raise Skip("freezable")  # the creator can freeze holders' tokens: a classic honeypot
        if mi and mi["bad_ext"]:
            raise Skip("token_ext", ext=mi["bad_ext"])  # transfer fees / hooks / permanent delegate
        paid = spent_usd(tr, self.sol_price())
        f["origin_usd"] = paid if paid > 0 else (f["amount"] * info["price_usd"] if info and info["price_usd"] else None)
        return f

    # ------------------------------------------------------------------ entry: shared facts, per-user decision
    def on_signal(self, wallet, token, amount_raw, blk, txh, net=None):
        net = net or self.nets["rh"]
        followers = self.db.followers(wallet)
        if not followers:
            return
        symbol = net.chain.symbol(token)
        users = {t["user_id"]: self.db.user(t["user_id"]) for t in followers}
        try:
            f = self.facts(wallet, token, amount_raw, blk, txh, [users[t["user_id"]] for t in followers], net)
        except Skip as s:
            for t in followers:
                self.log_skip(users[t["user_id"]], t, token, symbol, txh, blk, s, {"chain": net.key})
            return
        self._rr += 1
        k = self._rr % len(followers)
        for t in followers[k:] + followers[:k]:  # take turns: nobody is always second in line
            u = users[t["user_id"]]
            try:
                ticket, d = self.judge(u, t, token, f)
            except Skip as s:
                self.log_skip(u, t, token, symbol, txh, blk, s, f)
                continue
            self.buy(u, t, token, symbol, ticket, d, blk, txh)

    def facts(self, wallet, token, amount_raw, blk, txh, users, net=None):
        """Everything about the fill that does not depend on whose bot it is."""
        net = net or self.nets["rh"]
        f = {"chain": net.key, "age_s": round(time.time() - net.chain.block_ts(blk), 1)}
        tx = net.chain.tx(txh)
        f["tx_from"], f["tx_to"] = tx["from"].lower(), (tx.get("to") or "").lower()
        f["own"] = f["tx_from"] == wallet
        relay_fill = f["tx_to"] in self.relay.contracts_of(net.chain_id) or f["tx_from"] in self.relay.solvers_of(net.chain_id)
        if not f["own"] and not relay_fill:
            rc = net.chain.receipt(txh)
            if not any(lg["topics"] and lg["topics"][0] in SWAP_TOPICS for lg in rc["logs"]):
                raise Skip("not_a_swap", tx_from=f["tx_from"], tx_to=f["tx_to"])  # airdrop / dust push
        f["paid_eth"] = not f["own"] and int(tx.get("value") or "0x0", 16) > 0
        f["stock"] = net.key == "rh" and net.chain.name(token).endswith("Robinhood Token")
        f["rec"], f["pair"] = None, self.db.pairs(wallet)
        if not f["own"] and any(self.ucfg(u)["gates"].get("relay_gate", "strict") != "off" for u in users):
            f["rec"] = self.relay.wait_fill_record(txh, float(self.base_cfg["gates"].get("relay_wait_s", 1.5)))
        f["info"] = net.market.info(token, fresh=True)
        f["dec"] = net.chain.decimals(token)
        f["amount"] = amount_raw / 10 ** f["dec"]
        rec = f["rec"]
        f["origin_usd"] = rec.in_usd if rec and rec.referrer == "fomo" and rec.in_usd else None
        if f["origin_usd"] is None and f["info"] and f["info"]["price_usd"]:
            f["origin_usd"] = f["amount"] * f["info"]["price_usd"]
        return f

    def judge(self, u, t, token, f):
        cfg = self.ucfg(u)
        g, sz, ex = cfg["gates"], cfg["sizing"], cfg["execution"]
        now = time.time()
        chain = f.get("chain", "rh")
        d = {"age_s": f["age_s"], "own": f["own"], "chain": chain, "dec": f["dec"]}
        live = self.live_on(u, chain)
        if not (cfg.get("chains") or {}).get(CHAIN_KEY[chain], True) or (chain != "sol" and chain not in self.nets):
            raise Skip("chain_off")
        if u["paused"]:
            raise Skip("paused")
        if self.db.holding(u["id"], token):
            raise Skip("already_holding")
        if self.db.ever_rugged(u["id"], token):
            raise Skip("rugged_before")
        last = self.db.last_closed(u["id"], token)
        if last is not None and now - last < float(g.get("reentry_cooldown_hours", 24)) * 3600:
            raise Skip("cooldown")
        if len(self.db.positions(u["id"])) >= int(sz["max_positions"]):
            raise Skip("max_positions")
        if f["age_s"] > float(g["max_signal_age_s"]):
            raise Skip("stale", **d)
        if g.get("skip_stock_tokens", True) and f["stock"]:
            raise Skip("stock_token")
        if live:
            if chain == "sol":
                low = self.sol_rpc.sol_balance(self.sol_address(u)) / 1e9 < float((self.base_cfg.get("solana") or {}).get("min_sol", 0.02))
                coin = "SOL"
            else:
                net = self.nets[chain]
                low = net.chain.eth_balance(self.wallet_address(u)) / 1e18 < net.min_gas
                coin = "ETH"
            if low:
                if now - self._gas_alert.get((u["id"], chain), 0) > 3600:
                    self._gas_alert[(u["id"], chain)] = now
                    self.notify(u, f"⚠ Мало {coin} на комиссии ({CHAIN_NAME[chain]}) — новые покупки там остановлены, "
                                   "выходы работают. Пополни кошелёк.")
                raise Skip("low_gas")
        if g.get("plant_gate", True) and f["paid_eth"]:
            raise Skip("plant_eth", **d)
        mode = g.get("relay_gate", "strict")
        if not f["own"] and mode != "off":
            rec = f["rec"]
            if rec is None:
                if g.get("relay_unknown", "buy") == "skip":
                    raise Skip("relay_unknown", **d)
                d["relay"] = "unknown"
            else:
                d.update(referrer=rec.referrer, signer=rec.signer)
                if rec.referrer != "fomo" or rec.origin_chain != SOLANA_CHAIN_ID:
                    raise Skip("not_fomo_fill", **d)
                if mode == "strict":
                    if not f["pair"]:
                        raise Skip("no_pair", **d)
                    if rec.signer not in f["pair"]:
                        raise Skip("planted", **d)
        info = f["info"]
        if not info:
            if g.get("require_market_data", True):
                raise Skip("no_market_data", **d)
            info = {"price_usd": 0, "liquidity_usd": 0, "mcap_usd": 0, "pair_created_ms": 0, "buys_h24": 0, "sells_h24": 0}
        origin_usd = f["origin_usd"]
        d.update(origin_usd=round(origin_usd or 0, 2), liq=round(info["liquidity_usd"]), mcap=round(info["mcap_usd"]))
        if origin_usd is not None and origin_usd < float(g["min_origin_usd"]):
            raise Skip("dust_origin", **d)
        if info["liquidity_usd"] < float(g["min_liquidity_usd"]):
            raise Skip("low_liquidity", **d)
        if g.get("min_mcap_usd") and info["mcap_usd"] < float(g["min_mcap_usd"]):
            raise Skip("mcap_low", **d)
        if g.get("max_mcap_usd") and info["mcap_usd"] > float(g["max_mcap_usd"]):
            raise Skip("mcap_high", **d)
        if info["pair_created_ms"]:
            d["pool_age_min"] = round((now * 1000 - info["pair_created_ms"]) / 60000, 1)
            if d["pool_age_min"] < float(g.get("min_pool_age_minutes", 0)):
                raise Skip("young_pool", **d)
        if info["buys_h24"] >= int(g.get("honeypot_min_buys", 30)):
            if info["sells_h24"] / max(1, info["buys_h24"]) < float(g.get("honeypot_min_sell_ratio", 0.15)):
                raise Skip("sell_ratio", **d)
        ticket = strategy.ticket_size(sz, t.get("ticket_usd"), self.equity(u, chain), info["liquidity_usd"])
        d["ticket"] = ticket
        if ticket < float(sz.get("min_ticket_usd", 3)):
            raise Skip("ticket_too_small", **d)
        if self.cash(u, chain) < ticket:
            raise Skip("no_cash", **d)
        if chain == "sol":
            return ticket, self.judge_sol_quotes(token, ticket, f, g, d)
        net = self.nets[chain]
        exe = self.exe(u, live, chain)
        q = self.relay.quote(exe.addr, net.usd, token, int(ticket * 10 ** net.usd_decimals),
                             int(ex["buy_slippage_bps"]), chain_id=net.chain_id)
        if not q or q.out_raw <= 0:
            raise Skip("no_route", err=self.relay.last_error, **d)
        dec = f["dec"]
        if origin_usd and f["amount"] > 0:
            d["chase"] = round((ticket / (q.out_raw / 10 ** dec) / (origin_usd / f["amount"]) - 1) * 100, 1)
            if d["chase"] > float(g["max_chase_pct"]):
                raise Skip("chase", **d)
        d["impact"] = q.impact_pct
        if -q.impact_pct > float(g["max_price_impact_pct"]):
            raise Skip("impact", **d)
        d["gas_pct"] = round(q.gas_usd / ticket * 100, 2)
        if d["gas_pct"] > max(float(g.get("max_gas_pct", 1.0)), net.max_gas_pct):
            raise Skip("gas", **d)
        back = self.relay.quote(exe.addr, token, net.usd, q.out_raw, int(ex["sell_slippage_bps"]), chain_id=net.chain_id)
        if not back or back.out_raw <= 0:
            raise Skip("no_exit_route", **d)
        d["round_trip_loss"] = round((1 - back.out_raw / 10 ** net.usd_decimals / ticket) * 100, 2)
        if d["round_trip_loss"] > float(g["max_round_trip_loss_pct"]):
            raise Skip("round_trip", **d)
        return ticket, d

    def judge_sol_quotes(self, mint, ticket, f, g, d):
        q = self.jup.order(SOL_USDC, mint, int(ticket * 1e6))  # quote only: no wallet needed
        if not q or int(q.get("outAmount") or 0) <= 0:
            raise Skip("no_route", err=self.jup.last_error, **d)
        out = int(q["outAmount"]) / 10 ** f["dec"]
        if f["origin_usd"] and f["amount"] > 0:
            d["chase"] = round((ticket / out / (f["origin_usd"] / f["amount"]) - 1) * 100, 1)
            if d["chase"] > float(g["max_chase_pct"]):
                raise Skip("chase", **d)
        d["impact"] = round(float(q.get("priceImpact") or 0), 2)
        if -d["impact"] > float(g["max_price_impact_pct"]):
            raise Skip("impact", **d)
        scfg = self.base_cfg.get("solana") or {}
        d["gas_pct"] = round(float(scfg.get("est_fee_sol", 0.0002)) * self.sol_price() / ticket * 100, 2)
        if d["gas_pct"] > float(g.get("max_gas_pct", 1.0)):
            raise Skip("gas", **d)
        if scfg.get("round_trip_check"):
            back = self.jup.order(mint, SOL_USDC, int(q["outAmount"]))
            if not back or int(back.get("outAmount") or 0) <= 0:
                raise Skip("no_exit_route", **d)
            d["round_trip_loss"] = round((1 - int(back["outAmount"]) / 1e6 / ticket) * 100, 2)
            if d["round_trip_loss"] > float(g["max_round_trip_loss_pct"]):
                raise Skip("round_trip", **d)
        return d

    def log_skip(self, u, t, token, symbol, txh, blk, s, f=None):
        """Record the skip with the facts needed to judge it later (price, cap, chain), and report it."""
        f = f or {}
        d = dict(s.details)
        chain = d.get("chain") or f.get("chain") or "rh"
        d["chain"] = chain
        info = f.get("info") or {}
        if info.get("price_usd"):
            d.setdefault("price", info["price_usd"])
            d.setdefault("mcap", round(info.get("mcap_usd") or 0))
        if f.get("origin_usd"):
            d.setdefault("origin_usd", round(f["origin_usd"], 2))
        self.db.signal(u["id"], t["address"], t["label"], token, symbol, txh, blk, "skipped", s.reason, d)
        self.log.info("[%s] skip %s from %s: %s %s", u["name"], symbol, t["label"], s.reason, d)
        if not (self.tg and u.get("notify_skips")) or s.reason in NOISE or s.reason in json.loads(u.get("muted") or "[]"):
            return
        head = f"⏭ ПРОПУСК {symbol} ({CHAIN_NAME[chain]}) · {t['label']}"
        trade = []
        if d.get("origin_usd"):
            trade.append(f"трейдер купил на {usd(d['origin_usd'])}")
        if d.get("mcap"):
            trade.append(f"капа {big(d['mcap'])}")
        text = head + ("\n" + " · ".join(trade) if trade else "") + "\n" + skip_detail(s.reason, d, self.ucfg(u)["gates"])
        kb = [[{"text": "📈 DexScreener", "url": f"https://dexscreener.com/{DEX_PATH[chain]}/{token}"},
               B("🔕 Не присылать такие", f"mute:{s.reason}")]]
        self.tg.send(u["chat_id"], text, kb=kb)

    def buy(self, u, t, token, symbol, ticket, d, blk, txh):
        chain = d.get("chain", "rh")
        live = self.live_on(u, chain)
        cfg = self.ucfg(u)
        try:
            if chain == "sol":
                fill = self.sol_exe(u, live).buy(token, ticket)
            else:
                fill = self.exe(u, live, chain).buy(token, ticket, int(cfg["execution"]["buy_slippage_bps"]))
        except (ExecError, SolError) as e:
            self.db.signal(u["id"], t["address"], t["label"], token, symbol, txh, blk, "failed", str(e), d)
            return self.notify(u, f"⚠ покупка {symbol} ({CHAIN_NAME[chain]}) не прошла: {e}")
        dec = d.get("dec") if d.get("dec") is not None else self.chain.decimals(token)
        state = {"origin_tx": txh}
        if d.get("relay") == "unknown":
            state["recheck_until"] = time.time() + float(cfg["gates"].get("relay_watch_s", 120))
        source = d.get("source", "wallet")
        pid = self.db.open_position(
            user_id=u["id"], chain=chain, token=token, symbol=symbol, decimals=dec, wallet=t["address"], label=t["label"],
            opened=time.time(), status="open", paper=0 if live else 1, cost_usd=fill.usd, source=source,
            tokens_initial=str(fill.tokens_raw), tokens_left=str(fill.tokens_raw),
            entry_price=fill.usd / (fill.tokens_raw / 10 ** dec), gas_usd=fill.gas_usd,
            last_price=fill.usd / (fill.tokens_raw / 10 ** dec), peak_price=fill.usd / (fill.tokens_raw / 10 ** dec),
            state=json.dumps(state))
        self.db.fill(pid, "buy", fill.tokens_raw, fill.usd, fill.gas_usd, fill.tx, "entry")
        if not live:
            self.add_paper_cash(u["id"], -fill.usd)
        self.db.signal(u["id"], t["address"], t["label"], token, symbol, txh, blk, "bought", "", d, source=source)
        self.notify(u, f"🟢 КУПИЛ {symbol} ({CHAIN_NAME[chain]}) на {usd(fill.usd)} вслед за {t['label']}"
                       f"{'' if live else ' [paper]'}\n"
                       f"пул {big(d.get('liq', 0))} · капа {big(d.get('mcap', 0))} · догон {d.get('chase', 0):+.1f}%",
                    kb=[[B("📄 Позиция", f"p:{pid}"), B("🔴 Продать всё", f"ps:{pid}:100")],
                        [{"text": "📈 DexScreener", "url": f"https://dexscreener.com/{DEX_PATH[chain]}/{token}"}]])

    # ------------------------------------------------------------------ exits
    def recheck_provenance(self):
        """Positions bought before Relay had indexed the trader's fill (~5-7 s): verify every second,
        and dump at once if the fill turns out to be planted."""
        now = time.time()
        for p in self.db.positions():
            st = p["state"]
            if not st.get("recheck_until") or now < st.get("recheck_next", 0):
                continue
            st["recheck_next"] = now + 1.0
            u = self.db.user(p["user_id"])
            rec = self.relay.fill_record(st["origin_tx"])
            if rec:
                mode = self.ucfg(u)["gates"].get("relay_gate", "strict")
                bad = rec.referrer != "fomo" or rec.origin_chain != SOLANA_CHAIN_ID or \
                    (mode == "strict" and rec.signer not in self.db.pairs(p["wallet"]))
                st.pop("recheck_until", None)
                if bad:
                    st["planted"] = True
                    self.notify(u, f"✖ {p['symbol']}: покупка трейдера оказалась подставной "
                                   f"(подписал {rec.signer[:8]}…) — сливаю позицию")
            elif now > st["recheck_until"]:
                st.pop("recheck_until", None)
            self.db.update_position(p["id"], state=json.dumps(st))
            if st.get("planted"):
                p["state"] = st
                self.do_sell(u, p, "planted", p["tokens_left"], {})

    def update_positions(self):
        ops = self.db.positions()
        if not ops:
            return
        prices = {}
        for net in self.nets.values():
            prices.update(net.market.prices(list({p["token"] for p in ops if (p.get("chain") or "rh") == net.key})))
        prices.update(self.market_sol.prices(list({p["token"] for p in ops if p.get("chain") == "sol"})))
        now = time.time()
        users = {}
        for p in ops:
            price = prices.get(p["token"])
            if price:
                self.db.update_position(p["id"], last_price=price, peak_price=max(price, p["peak_price"] or 0))
                p["last_price"] = price
            if p["state"].get("next_try") and now < p["state"]["next_try"]:
                continue
            u = users.setdefault(p["user_id"], self.db.user(p["user_id"]))
            plan = strategy.exit_plan(p, price, now, self.ucfg(u)["exits"])
            if plan:
                self.do_sell(u, p, *plan)

    def do_sell(self, u, p, reason, amount, upd):
        ex, st = self.ucfg(u)["execution"], p["state"]
        chain = p.get("chain") or "rh"
        live = not p["paper"]
        if chain != "sol" and chain not in self.nets:
            return  # that network is switched off in config.yaml; the position waits
        if live and not (self.sol_key(u) if chain == "sol" else self.user_key(u)):
            return self.notify(u, f"⚠ {p['symbol']}: нет ключа кошелька, продать не могу")
        fails = int(st.get("sell_fails", 0))
        slip = min(float(ex["sell_slippage_bps"]) * 1.5 ** fails, float(ex["sell_slippage_max_bps"]))
        exe = self.sol_exe(u, live) if chain == "sol" else self.exe(u, live, chain)
        try:
            fill = exe.sell(p["token"], amount) if chain == "sol" else exe.sell(p["token"], amount, int(slip))
        except (ExecError, SolError) as e:
            now = time.time()
            st["sell_fails"] = fails + 1
            st.setdefault("first_fail", now)
            st["next_try"] = now + min(300, 10 * 2 ** fails)
            if st["sell_fails"] >= int(ex.get("unsellable_failures", 12)) and \
                    now - st["first_fail"] >= float(ex.get("unsellable_hours", 3)) * 3600:
                self.db.update_position(p["id"], status="writeoff", closed=now, state=json.dumps(st))
                return self.notify(u, f"✖ {p['symbol']} списана: {st['sell_fails']} неудачных продаж ({e})")
            self.db.update_position(p["id"], state=json.dumps(st))
            if fails == 0:
                self.notify(u, f"⚠ продажа {p['symbol']} ({exit_reason(reason)}) не прошла: {e} — "
                               f"повторю с большим проскальзыванием", kb=[[B("📄 Позиция", f"p:{p['id']}")]])
            return
        left = max(0, p["tokens_left"] - fill.tokens_raw)
        if live:
            onchain = self.sol_rpc.token_balance(exe.addr, p["token"]) if chain == "sol" \
                else self.nets[chain].chain.erc20_balance(p["token"], exe.addr)
            left = min(left, onchain)
        realized = (p["realized_usd"] or 0) + fill.usd
        gas = (p["gas_usd"] or 0) + fill.gas_usd
        st.update(upd)
        for k in ("sell_fails", "first_fail", "next_try", "manual", "manual_frac"):
            st.pop(k, None)
        price = p["last_price"] or 0
        dust = left <= p["tokens_initial"] * 0.001 or (price and left / 10 ** p["decimals"] * price < 0.25)
        fields = dict(tokens_left=str(left), realized_usd=realized, gas_usd=gas, state=json.dumps(st))
        if dust:
            fields.update(status="closed", closed=time.time())
            if live and chain == "sol" and left == 0:
                try:  # give back the ~0.002 SOL deposit of the emptied token account
                    exe.close_empty_accounts(p["token"])
                except Exception as e:
                    self.log.warning("close token account %s: %s", p["symbol"], e)
        self.db.update_position(p["id"], **fields)
        self.db.fill(p["id"], "sell", fill.tokens_raw, fill.usd, fill.gas_usd, fill.tx, reason)
        if not live:
            self.add_paper_cash(u["id"], fill.usd)
        msg = f"🔴 ПРОДАЛ {p['symbol']} ({CHAIN_NAME[chain]}) на {usd(fill.usd)} · {exit_reason(reason)}"
        if dust:
            pnl = realized - p["cost_usd"] - gas
            msg += f"\nпозиция закрыта: PnL {usd(pnl)} ({pnl / p['cost_usd'] * 100:+.1f}%)"
        self.notify(u, msg + ("" if live else " [paper]"), kb=[[B("📄 Позиция", f"p:{p['id']}")]])

    # ------------------------------------------------------------------ traders' Solana pairs (shared)
    def learn_pair(self, address, label=None):
        label = label or address[:10]
        cands, total = self.relay.learn_pair(address)
        if not cands:
            return f"{label}: у этого кошелька пока нет покупок через FOMO на Relay"
        (top, n), rest = cands[0], [c for c in cands[1:] if c[1] >= 2]
        if n >= 3 and n / total >= 0.6:
            self.db.set_pair(address, top)
            msg = f"🔗 {label}: Solana-кошелёк {top} ({n} из {total} сделок)"
            if rest:
                msg += ("\nещё подписывал сделки: " + ", ".join(f"{c[0]} ({c[1]})" for c in rest) +
                        f"\nесли это тот же трейдер: /pair {label} {top},{rest[0][0]}")
            return msg
        return (f"{label}: не уверен — " + ", ".join(f"{c[0]} ({c[1]})" for c in cands[:3]) +
                f" из {total}. Проверь /find в copyfomo и задай вручную: /pair {label} АДРЕС")

    # ------------------------------------------------------------------ CLI helpers
    def status_text(self, u):
        ops = self.db.positions(u["id"])
        st = self.db.stats(u["id"])
        pnl = sum((s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) for s in st)
        live = [c.upper() for c in ("rh", "eth", "bsc", "base", "sol") if self.live_on(u, c)]
        return (f"{u['name']}: LIVE {','.join(live) or 'none (paper)'}"
                f"{' · PAUSED' if u['paused'] else ''} · "
                f"cash {usd(self.cash(u))} · open {len(ops)} ({usd(self.open_value(u))}) · "
                f"closed {len(st)} PnL {usd(pnl)} · traders {len(self.db.traders(u['id'], True))}")

    def route_report(self, token, ticket):
        token = token.lower()
        sym, dec = self.chain.symbol(token), self.chain.decimals(token)
        info = self.market.info(token, fresh=True) or {}
        lines = [f"{sym} {token}",
                 f"liq {big(info.get('liquidity_usd', 0))} · mcap {big(info.get('mcap_usd', 0))} · "
                 f"buys/sells 24h {info.get('buys_h24', '?')}/{info.get('sells_h24', '?')} · dex {info.get('dex', '?')}"]
        q = self.relay.quote("0x000000000000000000000000000000000000dEaD", self.usdg, token, int(ticket * 1e6), 300)
        if not q:
            return "\n".join(lines + [f"no buy route: {self.relay.last_error}"])
        back = self.relay.quote("0x000000000000000000000000000000000000dEaD", token, self.usdg, q.out_raw, 600)
        lines.append(f"buy {usd(ticket)} -> {q.out_raw / 10 ** dec:,.2f} {sym} · impact {q.impact_pct}% · "
                     f"gas≈{usd(q.gas_usd)} · relay fee {usd(q.relayer_usd)}")
        if back:
            lines.append(f"sell back -> {usd(back.out_raw / 1e6)} · round trip {(back.out_raw / 1e6 / ticket - 1) * 100:+.2f}%")
        return "\n".join(lines)
