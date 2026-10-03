"""Button interface (Telegram inline keyboards), several users. Screens are (text, keyboard) pairs;
a button press edits the same message, like copyfomo. Every screen is built for the user pressing it,
and every action checks that the object belongs to that user."""
import json
import time

from eth_account import Account

from .fmt import NOISE, SKIP_RU, age, big, usd
from .sol import USDC as SOL_USDC, is_sol_addr, new_keypair


def B(text, data):
    return {"text": text, "callback_data": data}


def U(text, url):
    return {"text": text, "url": url}


MAIN_BTN, POS_BTN, TR_BTN, SET_BTN = "📊 Меню", "💼 Позиции", "👥 Трейдеры", "⚙️ Настройки"
REPLY_KB = {"keyboard": [[{"text": MAIN_BTN}, {"text": POS_BTN}], [{"text": TR_BTN}, {"text": SET_BTN}]],
            "resize_keyboard": True, "is_persistent": True}
EXPLORER = "https://robin.etherscan.io"
SOLSCAN = "https://solscan.io"
CHAIN_NAME = {"rh": "Robinhood", "sol": "Solana", "eth": "Ethereum"}
DEX_PATH = {"rh": "robinhood", "sol": "solana", "eth": "ethereum"}
ETHERSCAN = "https://etherscan.io"
TAG = {"rh": "RH", "eth": "ETH", "sol": "SOL"}

LADDERS = {
    "mine": ("+50%→50%, +300%→25%", [{"at_pct": 50, "sell": 0.5}, {"at_pct": 300, "sell": 0.25}]),
    "fast": ("+30%→50%, +100%→50%", [{"at_pct": 30, "sell": 0.5}, {"at_pct": 100, "sell": 0.5}]),
    "moon": ("+100%→50%, остальное едет", [{"at_pct": 100, "sell": 0.5}]),
}
TIMED = {
    "qorb": ("5м→75%, 10м→25%", [{"after_min": 5, "sell": 0.75}, {"after_min": 10, "sell": 0.25}]),
    "fast": ("2м→50%, 5м→50%", [{"after_min": 2, "sell": 0.5}, {"after_min": 5, "sell": 0.5}]),
    "slow": ("15м→50%, 60м→50%", [{"after_min": 15, "sell": 0.5}, {"after_min": 60, "sell": 0.5}]),
}
SECTION_OF = {"sizing": "size", "exits": "exit", "gates": "filt", "execution": "filt", "chains": "net"}
LABELS = {"sizing.ticket_usd": "тикет, $", "sizing.max_positions": "макс. позиций",
          "exits.stop_loss_pct": "стоп-лосс, % (0 = без стопа)", "gates.max_chase_pct": "догон, %",
          "exits.ladder": "лесенку", "exits.timed": "таймер"}

HELP = """Всё управление — кнопками: 📊 Меню внизу экрана.

Команды для продвинутых:
/menu — главное меню
/pause · /resume — стоп/старт новых покупок (выходы работают всегда)
/add 0xАДРЕС ник · /learnpair ник
/config · /get ключ · /set ключ значение   (пример: /set exits.stop_loss_pct 40)
/pair ник SOL[,SOL2] — только владелец бота"""


def ladder_str(ladder):
    return ", ".join(f"+{r['at_pct']}%→{float(r['sell']) * 100:.0f}%" for r in ladder or []) or "—"


def timed_str(timed):
    return ", ".join(f"{t['after_min']}м→{float(t['sell']) * 100:.0f}%" for t in timed or []) or "—"


class UI:
    def __init__(self, bot):
        self.b = bot
        self.awaiting = {}   # user id -> ("add"|"set"|"wd", key)
        self.pending = {}    # user id -> prepared withdrawal

    # ================================================================ entry points
    def send(self, u, text, kb=None, **kw):
        self.b.tg.send(u["chat_id"], text, kb=kb, **kw)

    def push(self, u, screen):
        self.send(u, screen[0], kb=screen[1])

    def on_text(self, u, text):
        b = self.b
        if text == "/start":
            mode = ("Режим LIVE: сделки идут на реальные деньги с твоего кошелька." if b.is_live(u) else
                    "Ты в paper-режиме: сделки считаются по реальным котировкам, но без денег.")
            self.send(u, f"Привет, {u['name']}! Кнопки внизу — главное меню, оно всегда под рукой.\n{mode}",
                      reply_kb=REPLY_KB)
            return self.push(u, self.main(u))
        if text in (MAIN_BTN, "/menu", "/status"):
            self.awaiting.pop(u["id"], None)
            return self.push(u, self.main(u))
        if text == POS_BTN:
            return self.push(u, self.positions(u))
        if text == TR_BTN:
            return self.push(u, self.traders(u))
        if text == SET_BTN:
            return self.push(u, self.settings(u))
        if u["id"] in self.awaiting and not text.startswith("/"):
            return self.on_input(u, text)
        self.awaiting.pop(u["id"], None)
        self.send(u, self.command(u, text))

    def command(self, u, text):
        b, db = self.b, self.b.db
        parts = text.split()
        cmd, args = parts[0].lower().split("@")[0], parts[1:]
        if cmd == "/help":
            return HELP
        if cmd in ("/pause", "/resume"):
            db.update_user(u["id"], paused=1 if cmd == "/pause" else 0)
            return "новые покупки на паузе (выходы работают)" if cmd == "/pause" else "работаю"
        if cmd == "/set" and len(args) >= 2:
            return b.set_override(u, args[0], " ".join(args[1:]))
        if cmd == "/get" and args:
            return json.dumps(b.get_key(u, args[0]), ensure_ascii=False)
        if cmd == "/config":
            cfg = b.ucfg(u)
            return json.dumps({k: cfg[k] for k in ("sizing", "gates", "exits", "execution")}, indent=1, ensure_ascii=False)
        if cmd == "/add" and args and is_sol_addr(args[0]):
            db.add_trader(u["id"], args[0], args[1] if len(args) > 1 else args[0][:6])
            db.set_pair(args[0], args[0])
            return "добавлен Solana-трейдер"
        if cmd == "/add" and args and args[0].lower().startswith("0x") and len(args[0]) == 42:
            db.add_trader(u["id"], args[0], args[1] if len(args) > 1 else args[0][:8])
            return f"добавлен {args[1] if len(args) > 1 else args[0][:8]}\n" + self._pair_note(args[0])
        if cmd == "/learnpair" and args:
            t = db.trader(u["id"], args[0])
            return b.learn_pair(t["address"], t["label"]) if t else "нет такого трейдера"
        if cmd == "/pair" and len(args) == 2:
            if u["id"] != 1:
                return "только владелец бота задаёт Solana-пары вручную"
            t = db.trader(u["id"], args[0])
            if not t:
                return "нет такого трейдера"
            db.set_pair(t["address"], args[1])
            return f"{t['label']}: Solana {args[1]}"
        return "Не знаю такой команды. Всё есть в кнопках — /menu"

    def on_callback(self, u, it):
        try:
            screen, toast = self.route(u, it["data"])
        except Exception as e:
            self.b.log.exception("ui %s: %s", it["data"], e)
            screen, toast = None, f"ошибка: {e}"[:190]
        self.b.tg.answer(it["cb_id"], toast)
        if screen and it.get("msg_id"):
            self.b.tg.edit(u["chat_id"], it["msg_id"], *screen)

    # ================================================================ routing
    def route(self, u, d):
        b, db, uid = self.b, self.b.db, u["id"]
        cmd, _, arg = d.partition(":")
        if cmd == "noop":
            return None, None
        if cmd == "m":
            return self.main(u), None
        if cmd == "pause":
            db.update_user(uid, paused=0 if u["paused"] else 1)
            u = db.user(uid)
            return self.main(u), ("пауза: новые покупки остановлены, выходы работают" if u["paused"] else "работаю")
        # --- positions
        if cmd == "pos":
            return self.positions(u), None
        if cmd == "p":
            return self.position(u, int(arg)), None
        if cmd == "ps":
            pid, pct = arg.split(":")
            return self.confirm_sell(u, int(pid), int(pct)), None
        if cmd == "psc":
            pid, pct = arg.split(":")
            return self.manual_sell(u, int(pid), int(pct))
        # --- traders
        if cmd == "tr":
            return self.traders(u), None
        if cmd == "t":
            return self.trader(u, arg), None
        if cmd == "tt":
            t = db.trader(uid, arg)
            db.set_trader(uid, t["address"], active=0 if t["active"] else 1)
            return self.trader(u, arg), ("копирование выключено" if t["active"] else "копирую")
        if cmd == "tk":
            addr, v = arg.rsplit(":", 1)
            db.set_trader(uid, addr, ticket_usd=float(v) or None)
            return self.trader(u, addr), "тикет сохранён"
        if cmd == "tl":
            t = db.trader(uid, arg)
            return self.trader(u, arg, note=b.learn_pair(t["address"], t["label"])), None
        if cmd == "td":
            t = db.trader(uid, arg)
            return (f"Удалить {t['label']}?\nОткрытые позиции останутся и закроются по твоим выходам.",
                    [[B("🗑 Да, удалить", f"tdc:{arg}"), B("❌ Нет", f"t:{arg}")]]), None
        if cmd == "tdc":
            db.delete_trader(uid, arg)
            return self.traders(u), "удалён"
        if cmd == "tadd":
            self.awaiting[uid] = ("add", None)
            self.send(u, "Пришли адрес трейдера и ник одним сообщением:\n0x… Ник\n\n"
                         "Адрес FOMO-трейдера берётся в copyfomo: /find ник — бот сам найдёт и его Solana-кошелёк.\n"
                         "Можно прислать и просто Solana-адрес любого трейдера — тогда копирую только в Solana.")
            return None, None
        # --- settings
        if cmd == "set":
            return self.settings(u), None
        if cmd == "s":
            return self.section(u, arg), None
        if cmd == "v":
            key, _, raw = arg.partition(":")
            b.set_override(u, key, raw)
            return self.section(u, SECTION_OF[key.split(".")[0]]), "сохранено"
        if cmd == "tog":
            b.set_override(u, arg, json.dumps(not bool(b.get_key(u, arg))))
            return self.section(u, SECTION_OF[arg.split(".")[0]]), "сохранено"
        if cmd == "lad":
            b.set_override(u, "exits.ladder", json.dumps(LADDERS[arg][1]))
            return self.section(u, "exit"), "лесенка сохранена"
        if cmd == "tim":
            b.set_override(u, "exits.timed", json.dumps(TIMED[arg][1]))
            return self.section(u, "exit"), "таймер сохранён"
        if cmd == "c":
            self.awaiting[uid] = ("set", arg)
            hint = {"exits.ladder": "пары «рост:продать%», например: 50:50 300:25",
                    "exits.timed": "пары «минуты:продать%», например: 5:75 10:25"}.get(arg, "число")
            self.send(u, f"Введи {LABELS.get(arg, arg)} — {hint}.\nСейчас: {json.dumps(b.get_key(u, arg), ensure_ascii=False)}")
            return None, None
        if cmd == "skips":
            db.update_user(uid, notify_skips=0 if u["notify_skips"] else 1)
            return self.section(db.user(uid), "notif"), "сохранено"
        if cmd in ("mute", "unmute"):
            muted = set(json.loads(u.get("muted") or "[]"))
            (muted.add if cmd == "mute" else muted.discard)(arg)
            db.update_user(uid, muted=json.dumps(sorted(muted)))
            if cmd == "mute":
                return None, f"больше не присылаю: {SKIP_RU.get(arg, arg)}"
            return self.section(db.user(uid), "notif"), "снова присылаю"
        if cmd == "miss":
            return self.missed(u), None
        # --- stats
        if cmd == "st":
            return self.stats(u), None
        if cmd == "cl":
            return self.closed(u), None
        # --- wallet
        if cmd == "w":
            return self.wallets(u), None
        if cmd == "wr":
            return self.wallet(u), None
        if cmd == "wsl":
            return self.wallet_sol(u), None
        if cmd == "weth":
            return self.wallet_eth(u), None
        if cmd == "elive":
            if arg == "on":
                if not b.user_key(u):
                    return self.wallet_eth(u), "сначала создай кошелёк"
                net, addr = b.nets.get("eth"), b.wallet_address(u)
                usdc = net.chain.erc20_balance(net.usd, addr) / 1e6 if net else 0
                eth = net.chain.eth_balance(addr) / 1e18 if net else 0
                return (f"🔴 Включить LIVE в Ethereum?\n\nПокупки пойдут с кошелька {addr} на реальные деньги.\n"
                        f"Баланс в Ethereum: {usdc:.2f} USDC, {eth:.5f} ETH\n"
                        "Нужны USDC (на сделки) и ETH на газ — от 0.002 ETH. Газ в Ethereum дороже, чем в Robinhood: "
                        "тикет лучше от $25.",
                        [[B("🔴 Да, включить", "elivec"), B("❌ Нет", "weth")]]), None
            db.update_user(uid, eth_live=0)
            return self.wallet_eth(db.user(uid)), "Ethereum: paper-режим"
        if cmd == "elivec":
            if not b.user_key(u):
                return self.wallet_eth(u), "сначала создай кошелёк"
            db.update_user(uid, eth_live=1)
            return self.wallet_eth(db.user(uid)), "Ethereum: LIVE включён"
        if cmd == "wsc":
            if b.sol_key(u):
                return self.wallet_sol(u), "кошелёк уже есть"
            addr, secret = new_keypair()
            db.update_user(uid, sol_key=secret)
            return self.wallet_sol(db.user(uid), note="✅ Solana-кошелёк создан. Сразу сохрани ключ: «🔐 Показать ключ»."), None
        if cmd == "wsk":
            return ("🔐 Показать приватный ключ Solana-кошелька?\n\nКто знает ключ — распоряжается деньгами. Сообщение "
                    "удалится через 60 секунд. Ключ подходит для импорта в Phantom / Solflare.",
                    [[B("🔐 Да, показать", "wskc"), B("❌ Нет", "wsl")]]), None
        if cmd == "wskc":
            key = b.sol_key(u)
            if key:
                self.send(u, f"Ключ Solana-кошелька {b.sol_address(u)}:\n<tg-spoiler>{key}</tg-spoiler>\n\nУдалится через 60 секунд.",
                          extra={"parse_mode": "HTML", "protect_content": True}, delete_after=60)
            return self.wallet_sol(u), "отправил, удалится через минуту"
        if cmd == "wsd":
            self.awaiting[uid] = ("wd", arg)
            self.send(u, f"Куда и сколько вывести {'USDC' if arg == 'sol_usdc' else 'SOL'}? Пришли одним сообщением:\n"
                         "АДРЕС СУММА   или   АДРЕС all\n\nАдрес — твой кошелёк в сети Solana (Phantom, биржа и т.п.).")
            return None, None
        if cmd == "slive":
            if arg == "on":
                if not b.sol_key(u):
                    return self.wallet_sol(u), "сначала создай кошелёк"
                addr = b.sol_address(u)
                usdc = b.sol_rpc.token_balance(addr, SOL_USDC) / 1e6
                sol = b.sol_rpc.sol_balance(addr) / 1e9
                return (f"🔴 Включить LIVE в Solana?\n\nПокупки пойдут с кошелька {addr} на реальные деньги.\n"
                        f"Баланс: {usdc:.2f} USDC, {sol:.4f} SOL\n"
                        "Нужны USDC (на сделки) и около 0.1 SOL (комиссии и залоги за токен-аккаунты). "
                        "Открытые paper-позиции доиграют в paper.",
                        [[B("🔴 Да, включить", "slivec"), B("❌ Нет", "wsl")]]), None
            db.update_user(uid, sol_live=0)
            return self.wallet_sol(db.user(uid)), "Solana: paper-режим"
        if cmd == "slivec":
            if not b.sol_key(u):
                return self.wallet_sol(u), "сначала создай кошелёк"
            db.update_user(uid, sol_live=1)
            return self.wallet_sol(db.user(uid)), "Solana: LIVE включён"
        if cmd == "wc":
            if b.user_key(u):
                return self.wallet(u), "кошелёк уже есть"
            acct = Account.create()
            k = acct.key.hex()
            db.update_user(uid, private_key=k if k.startswith("0x") else "0x" + k)
            return self.wallet(db.user(uid), note="✅ Кошелёк создан. Сразу сохрани ключ: кнопка «🔐 Показать ключ»."), None
        if cmd == "wk":
            return ("🔐 Показать приватный ключ?\n\nКто знает ключ — распоряжается деньгами. Сообщение с ключом "
                    "удалится через 60 секунд: перепиши его в менеджер паролей и никому не пересылай.",
                    [[B("🔐 Да, показать", "wkc"), B("❌ Нет", "wr")]]), None
        if cmd == "wkc":
            key = b.user_key(u)
            if key:
                self.send(u, f"Ключ кошелька {b.wallet_address(u)}:\n<tg-spoiler>{key}</tg-spoiler>\n\nУдалится через 60 секунд.",
                          extra={"parse_mode": "HTML", "protect_content": True}, delete_after=60)
            return self.wallet(u), "отправил, удалится через минуту"
        if cmd == "wd":
            self.awaiting[uid] = ("wd", arg)
            net_name = "Ethereum" if arg.startswith("eth_") else "Robinhood Chain"
            coin = {"usdg": "USDG", "eth_usdc": "USDC"}.get(arg, "ETH")
            self.send(u, f"Куда и сколько вывести {coin} ({net_name})? Пришли одним сообщением:\n"
                         f"0xАДРЕС СУММА   или   0xАДРЕС all\n\nАдрес — твой кошелёк в сети {net_name}.")
            return None, None
        if cmd == "wdc":
            return self.do_withdraw(u)
        if cmd == "live":
            if arg == "on":
                if not b.user_key(u):
                    return self.wallet(u), "сначала создай кошелёк"
                addr = b.wallet_address(u)
                usdg = b.chain.erc20_balance(b.usdg, addr) / 1e6
                eth = b.chain.eth_balance(addr) / 1e18
                return (f"🔴 Включить LIVE?\n\nПокупки пойдут с кошелька {addr} на реальные деньги.\n"
                        f"Баланс: {usdg:.2f} USDG, {eth:.5f} ETH\n"
                        "Нужны USDG (на сделки) и немного ETH (на газ). Открытые paper-позиции доиграют в paper.\n"
                        "Вернуться в paper можно в любой момент.",
                        [[B("🔴 Да, включить LIVE", "livec"), B("❌ Нет", "wr")]]), None
            db.update_user(uid, live=0)
            return self.wallet(db.user(uid)), "paper-режим"
        if cmd == "livec":
            if not b.user_key(u):
                return self.wallet(u), "сначала создай кошелёк"
            db.update_user(uid, live=1)
            return self.wallet(db.user(uid)), "LIVE включён"
        # --- admin
        if cmd in ("adm", "inv", "ud", "udc"):
            if uid != 1:
                return self.main(u), "только для владельца бота"
            if cmd == "adm":
                return self.admin(), None
            if cmd == "inv":
                code = b.new_invite()
                return self.admin(note=f"Код приглашения (действует 24 ч, один раз):\n/start {code}\n\n"
                                       "Пусть друг найдёт этого бота в Telegram и отправит ему эту строку."), None
            if cmd == "ud":
                x = db.user(int(arg))
                return (f"Отключить {x['name']}?\nЕго открытые позиции доиграют по его выходам, новых покупок не будет.",
                        [[B("🚫 Да, отключить", f"udc:{arg}"), B("❌ Нет", "adm")]]), None
            if cmd == "udc" and int(arg) != 1:
                db.update_user(int(arg), active=0)
                return self.admin(), "отключён"
        return self.main(u), None

    # ================================================================ custom input
    def on_input(self, u, text):
        b, db, uid = self.b, self.b.db, u["id"]
        kind, key = self.awaiting[uid]
        if kind == "add":
            parts = text.split()
            addr = parts[0] if parts else ""
            if is_sol_addr(addr):  # a Solana-only trader (not on FOMO): watched in Solana only
                self.awaiting.pop(uid)
                label = parts[1] if len(parts) > 1 else addr[:6]
                db.add_trader(uid, addr, label)
                db.set_pair(addr, addr)
                return self.push(u, self.trader(u, addr, note="🟣 Solana-трейдер: бот следит за его кошельком в Solana."))
            addr = addr.lower()
            if not (addr.startswith("0x") and len(addr) == 42):
                return self.send(u, "Нужен адрес трейдера: EVM (0x + 40 символов) или Solana. Пришли ещё раз: АДРЕС Ник")
            self.awaiting.pop(uid)
            label = parts[1] if len(parts) > 1 else addr[:8]
            db.add_trader(uid, addr, label)
            return self.push(u, self.trader(u, addr, note=self._pair_note(addr, label)))
        if kind == "wd":
            return self.prepare_withdraw(u, key, text)
        val = self.parse(key, text)
        if val is None:
            return self.send(u, "Не понял значение, попробуй ещё раз (или нажми 📊 Меню для отмены).")
        self.awaiting.pop(uid)
        b.set_override(u, key, json.dumps(val))
        self.push(u, self.section(db.user(uid), SECTION_OF[key.split(".")[0]]))

    def _pair_note(self, addr, label=None):
        if self.b.db.pairs(addr):
            return "🔗 Solana-кошелёк трейдера уже известен"
        return self.b.learn_pair(addr, label)

    @staticmethod
    def parse(key, text):
        t = text.replace(",", ".").replace("$", "").replace("%", "").strip()
        try:
            if key in ("exits.ladder", "exits.timed"):
                out = []
                for pair in t.split():
                    a, s = pair.split(":")
                    item = {"at_pct" if key == "exits.ladder" else "after_min": float(a), "sell": float(s) / 100}
                    if item["sell"] <= 0:
                        return None
                    out.append(item)
                if not out or sum(x["sell"] for x in out) > 1.0001:
                    return None
                return out
            v = float(t)
            if v < 0 or (v == 0 and key != "exits.stop_loss_pct"):
                return None
            return int(v) if v.is_integer() else v
        except ValueError:
            return None

    # ================================================================ withdrawals
    def prepare_withdraw(self, u, asset, text):
        b, uid = self.b, u["id"]
        parts = text.split()
        sol = asset in ("sol_usdc", "sol")
        if sol:
            return self.prepare_withdraw_sol(u, asset, parts)
        if len(parts) != 2 or not (parts[0].lower().startswith("0x") and len(parts[0]) == 42):
            return self.send(u, "Формат: 0xАДРЕС СУММА или 0xАДРЕС all")
        to, amt = parts[0], parts[1].lower().replace(",", ".")
        addr = b.wallet_address(u)
        if not addr:
            self.awaiting.pop(uid, None)
            return self.send(u, "Кошелька нет — создай его в «👛 Кошелёк».")
        if to.lower() == addr.lower():
            return self.send(u, "Это адрес самого бота. Нужен твой внешний кошелёк.")
        chain = "eth" if asset.startswith("eth_") else "rh"
        if chain not in b.nets:
            self.awaiting.pop(uid, None)
            return self.send(u, "Ethereum выключен в config.yaml.")
        net = b.nets[chain]
        exe = b.exe(u, True, chain)
        try:
            if asset in ("usdg", "eth_usdc"):
                bal = net.chain.erc20_balance(net.usd, addr)
                raw = bal if amt == "all" else int(float(amt) * 1e6)
                human = f"{raw / 1e6:.2f} {'USDG' if chain == 'rh' else 'USDC'} ({CHAIN_NAME[chain]})"
            else:
                bal = net.chain.eth_balance(addr)
                reserve = exe.eth_reserve(to)
                raw = bal - reserve if amt == "all" else int(float(amt) * 1e18)
                human = f"{raw / 1e18:.6f} ETH ({CHAIN_NAME[chain]})"
                if raw + reserve > bal:
                    raw = -1
        except ValueError:
            return self.send(u, "Не понял сумму.")
        if raw <= 0 or raw > bal:
            return self.send(u, "Не хватает баланса (для ETH учти газ на саму отправку). Попробуй меньшую сумму.")
        self.awaiting.pop(uid, None)
        self.pending[uid] = {"asset": asset, "to": to, "raw": raw, "human": human, "ts": time.time(), "chain": chain}
        warn = ""
        if asset in ("eth", "eth_eth") and any(not p["paper"] and (p.get("chain") or "rh") == chain for p in b.db.positions(uid)):
            warn = f"\n⚠ Есть открытые live-позиции в {CHAIN_NAME[chain]} — без ETH бот не сможет их продать."
        self.push(u, (f"Вывести {human}\nна {to}?{warn}",
                      [[B("✅ Да, вывести", "wdc"), B("❌ Нет", "weth" if chain == "eth" else "wr")]]))

    def prepare_withdraw_sol(self, u, asset, parts):
        b, uid = self.b, u["id"]
        if len(parts) != 2 or not is_sol_addr(parts[0]):
            return self.send(u, "Формат: АДРЕС СУММА или АДРЕС all (адрес в сети Solana)")
        to, amt = parts[0], parts[1].lower().replace(",", ".")
        addr = b.sol_address(u)
        if not addr:
            self.awaiting.pop(uid, None)
            return self.send(u, "Solana-кошелька нет — создай его в «👛 Кошелёк».")
        if to == addr:
            return self.send(u, "Это адрес самого бота. Нужен твой внешний кошелёк.")
        try:
            if asset == "sol_usdc":
                bal = b.sol_rpc.token_balance(addr, SOL_USDC)
                raw = bal if amt == "all" else int(float(amt) * 1e6)
                human = f"{raw / 1e6:.2f} USDC"
            else:
                bal = b.sol_rpc.sol_balance(addr)
                raw = bal - 10_000 if amt == "all" else int(float(amt) * 1e9)  # keep the fee
                human = f"{raw / 1e9:.6f} SOL"
        except ValueError:
            return self.send(u, "Не понял сумму.")
        if raw <= 0 or raw > bal:
            return self.send(u, "Не хватает баланса (для SOL учти комиссию самой отправки). Попробуй меньшую сумму.")
        self.awaiting.pop(uid, None)
        self.pending[uid] = {"asset": asset, "to": to, "raw": raw, "human": human, "ts": time.time()}
        warn = ""
        if asset == "sol" and any(not p["paper"] and p.get("chain") == "sol" for p in b.db.positions(uid)):
            warn = "\n⚠ Есть открытые live-позиции в Solana — без SOL бот не сможет их продать."
        self.push(u, (f"Вывести {human}\nна {to}?{warn}", [[B("✅ Да, вывести", "wdc"), B("❌ Нет", "wsl")]]))

    def do_withdraw(self, u):
        b, uid = self.b, u["id"]
        w = self.pending.pop(uid, None)
        if not w or time.time() - w["ts"] > 300:
            return self.wallets(u), "заявка устарела, начни заново"
        if w["asset"] in ("sol_usdc", "sol"):
            exe = b.sol_exe(u, True)
            try:
                sig = exe.send_token(SOL_USDC, w["to"], w["raw"]) if w["asset"] == "sol_usdc" else exe.send_sol(w["to"], w["raw"])
            except Exception as e:
                return self.wallet_sol(u, note=f"⚠ Вывод не прошёл: {e}"), "ошибка"
            return self.wallet_sol(u, note=f"✅ Отправил {w['human']}\n{SOLSCAN}/tx/{sig}"), "готово"
        chain = w.get("chain", "rh")
        exe = b.exe(u, True, chain)
        try:
            if w["asset"] in ("usdg", "eth_usdc"):
                h = exe.transfer_erc20(b.nets[chain].usd, w["to"], w["raw"])
            else:
                h = exe.send_eth(w["to"], w["raw"])
        except Exception as e:
            return (self.wallet_eth if chain == "eth" else self.wallet)(u, note=f"⚠ Вывод не прошёл: {e}"), "ошибка"
        if chain == "eth":
            return self.wallet_eth(u, note=f"✅ Отправил {w['human']}\n{ETHERSCAN}/tx/{h}"), "готово"
        return self.wallet(u, note=f"✅ Отправил {w['human']}\n{EXPLORER}/tx/{h}"), "готово"

    # ================================================================ screens
    def _pnl(self, p):
        val = p["tokens_left"] / 10 ** p["decimals"] * (p["last_price"] or 0)
        pnl = val + (p["realized_usd"] or 0) - p["cost_usd"] - (p["gas_usd"] or 0)
        return val, pnl, pnl / p["cost_usd"] * 100 if p["cost_usd"] else 0.0

    def _opts(self, u, label, key, values, fmt):
        cur = self.b.get_key(u, key)
        return [B(label, "noop")] + [B(("✅ " if cur == v else "") + fmt(v), f"v:{key}:{json.dumps(v)}") for v in values]

    def main(self, u):
        b, db = self.b, self.b.db
        ops, st = db.positions(u["id"]), db.stats(u["id"])
        pnl = sum((s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) for s in st)
        wins = sum(1 for s in st if (s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) > 0)
        lrh, lsol = b.live_on(u, "rh"), b.live_on(u, "sol")
        cfg = b.ucfg(u)
        leth = b.live_on(u, "eth")
        nets = [n for n, k in (("Robinhood", "robinhood"), ("Ethereum", "ethereum"), ("Solana", "solana"))
                if (cfg.get("chains") or {}).get(k, True)]
        if not lrh and not lsol and not leth:
            money = f"🟡 PAPER — без реальных денег\nКапитал: {usd(b.equity(u))} · кэш {usd(b.cash(u))}\n"
        else:
            money = (f"Robinhood: {'🔴 LIVE' if lrh else '🟡 paper'} · кэш {usd(b.cash(u, 'rh'))}\n"
                     f"Ethereum: {'🔴 LIVE' if leth else '🟡 paper'} · кэш {usd(b.cash(u, 'eth') if 'eth' in b.nets else 0)}\n"
                     f"Solana: {'🔴 LIVE' if lsol else '🟡 paper'} · кэш {usd(b.cash(u, 'sol'))}\n")
        val = sum(self._pnl(p)[0] for p in ops)
        text = (f"🤖 rhcopy · {u['name']}\n"
                f"{'⏸ Пауза — новые покупки остановлены' if u['paused'] else '▶️ Работает'} · сети: {', '.join(nets) or 'нет'}\n\n"
                + money +
                f"Открыто: {len(ops)} на {usd(val)}\n"
                f"Закрыто сделок: {len(st)} · PnL {usd(pnl)}" + (f" · win {wins / len(st) * 100:.0f}%" if st else "") + "\n"
                f"Копирую трейдеров: {len(db.traders(u['id'], active_only=True))}")
        kb = [[B(f"💼 Позиции ({len(ops)})", "pos"), B("👥 Трейдеры", "tr")],
              [B("📈 Статистика", "st"), B("📜 Закрытые", "cl")],
              [B("⚙️ Настройки", "set"), B("▶️ Продолжить" if u["paused"] else "⏸ Пауза", "pause")],
              [B("👛 Кошельки", "w"), B("🔄 Обновить", "m")]]
        if u["id"] == 1:
            kb.append([B("👤 Пользователи", "adm")])
        return text, kb

    def positions(self, u):
        ops = self.b.db.positions(u["id"])
        if not ops:
            return "💼 Открытых позиций нет.", [[B("🔄 Обновить", "pos"), B("⬅ Меню", "m")]]
        lines, kb = ["💼 Открытые позиции:\n"], []
        for p in ops:
            val, pnl, pct = self._pnl(p)
            icon = "🟢" if pnl >= 0 else "🔴"
            lines.append(f"{icon} {p['symbol']} · {usd(val)} · {pct:+.1f}% · {age((time.time() - p['opened']) / 60)}"
                         f" · {p['label']} · {TAG.get(p.get('chain') or 'rh', 'RH')}{'' if p['paper'] else ' · LIVE'}")
            kb.append([B(f"{icon} {p['symbol']} {pct:+.0f}%", f"p:{p['id']}")])
        kb.append([B("🔄 Обновить", "pos"), B("⬅ Меню", "m")])
        return "\n".join(lines), kb

    def _own(self, u, pid):
        p = self.b.db.position_by_id(pid)
        return p if p and p["user_id"] == u["id"] else None

    def position(self, u, pid):
        p = self._own(u, pid)
        if not p:
            return "Позиция не найдена.", [[B("⬅ Позиции", "pos")]]
        val, pnl, pct = self._pnl(p)
        st, ex = p["state"], self.b.ucfg(u)["exits"]
        mult = p["last_price"] / p["entry_price"] if p["entry_price"] and p["last_price"] else 0
        peak = p["peak_price"] / p["entry_price"] if p["entry_price"] and p["peak_price"] else 0
        sold = 100 - p["tokens_left"] / p["tokens_initial"] * 100 if p["tokens_initial"] else 0
        if st.get("any_tp") and ex.get("stop_after_tp") == "breakeven":
            stop = "безубыток"
        else:
            stop = f"−{ex['stop_loss_pct']}%" if ex.get("stop_loss_pct") else "выкл"
        ch = p.get("chain") or "rh"
        lines = [f"{'🟢' if pnl >= 0 else '🔴'} {p['symbol']} ({CHAIN_NAME[ch]}) — вслед за {p['label']}"
                 + (" [paper]" if p["paper"] else " [LIVE]"),
                 "",
                 f"Вложено: {usd(p['cost_usd'])}",
                 f"Сейчас стоит: {usd(val)} (продано {sold:.0f}%)",
                 f"Получено с продаж: {usd(p['realized_usd'] or 0)}",
                 f"PnL: {usd(pnl)} ({pct:+.1f}%), газ {usd(p['gas_usd'] or 0)}",
                 f"Цена: x{mult:.2f} от входа · пик x{peak:.2f}",
                 f"Держу: {age((time.time() - p['opened']) / 60)}",
                 f"Стоп: {stop}" + (" · сработали TP" if st.get("tp_done") else "")
                 + (" · трейдер выходит" if st.get("origin_exit") else "")]
        if p["status"] != "open":
            lines.append("Позиция закрыта" if p["status"] == "closed" else "Позиция списана")
        elif st.get("manual"):
            lines.append("⏳ ручная продажа в очереди")
        elif st.get("sell_fails"):
            lines.append(f"⚠ продажа не прошла {st['sell_fails']} раз — повторяю с большим проскальзыванием")
        kb = []
        if p["status"] == "open":
            kb.append([B("🔴 Продать 50%", f"ps:{pid}:50"), B("🔴 Продать всё", f"ps:{pid}:100")])
        kb.append([U("📈 DexScreener", f"https://dexscreener.com/{DEX_PATH[ch]}/{p['token']}")])
        kb.append([B("🔄 Обновить", f"p:{pid}"), B("⬅ Позиции", "pos")])
        return "\n".join(lines), kb

    def confirm_sell(self, u, pid, pct):
        p = self._own(u, pid)
        if not p:
            return "Позиция не найдена.", [[B("⬅ Позиции", "pos")]]
        what = "всю позицию" if pct >= 100 else f"{pct}% позиции"
        return f"Продать {what} {p['symbol']}?", [[B("✅ Да, продать", f"psc:{pid}:{pct}"), B("❌ Нет", f"p:{pid}")]]

    def manual_sell(self, u, pid, pct):
        p = self._own(u, pid)
        if not p or p["status"] != "open":
            return self.position(u, pid), "позиция уже закрыта"
        p["state"]["manual"] = True
        p["state"]["manual_frac"] = min(1.0, max(0.01, pct / 100))
        self.b.db.update_position(pid, state=json.dumps(p["state"]))
        self.b._last_prices = 0  # run exits on the next tick
        return self.position(u, pid), "продаю…"

    def traders(self, u):
        b, db = self.b, self.b.db
        ts = db.traders(u["id"])
        default = b.ucfg(u)["sizing"]["ticket_usd"]
        lines, kb = (["👥 Трейдеры:\n"] if ts else ["👥 Трейдеров пока нет — добавь первого."]), []
        for t in ts:
            n, wins, pnl = db.wallet_stats(u["id"], t["address"])
            icon = "✅" if t["active"] else "⏸"
            lines.append(f"{icon} {t['label']}{'' if db.pairs(t['address']) else ' ⚠️'}"
                         f"{' 🟣' if not t['address'].startswith('0x') else ''} · тикет {usd(t['ticket_usd'] or default)}"
                         + (f" · сделок: {n}, {usd(pnl)}" if n else " · сделок ещё нет"))
            kb.append([B(f"{icon} {t['label']}", f"t:{t['address']}")])
        if any(not db.pairs(t["address"]) for t in ts):
            lines.append("\n⚠️ не найден Solana-кошелёк — без него не отличить подставную покупку")
        kb.append([B("➕ Добавить трейдера", "tadd")])
        kb.append([B("⬅ Меню", "m")])
        return "\n".join(lines), kb

    def trader(self, u, addr, note=None):
        b, db = self.b, self.b.db
        t = db.trader(u["id"], addr)
        if not t:
            return "Трейдер не найден.", [[B("⬅ Трейдеры", "tr")]]
        a, tk = t["address"], t["ticket_usd"]
        n, wins, pnl = db.wallet_stats(u["id"], a)
        openn = sum(1 for p in db.positions(u["id"]) if p["wallet"] == a)
        sol_only = not a.startswith("0x")
        lines = [f"👤 {t['label']} — {'✅ копирую' if t['active'] else '⏸ не копирую'}",
                 "",
                 "EVM: — (только Solana)" if sol_only else f"EVM: {a}",
                 f"Solana: {', '.join(sorted(db.pairs(a))) or 'не найден ⚠️'}",
                 f"Тикет: {usd(tk) if tk else usd(b.ucfg(u)['sizing']['ticket_usd']) + ' (общий)'}",
                 f"Закрыто сделок: {n}" + (f" · win {wins / n * 100:.0f}% · PnL {usd(pnl)}" if n else ""),
                 f"Открыто сейчас: {openn}"]
        if note:
            lines += ["", note]
        kb = [[B("⏸ Не копировать" if t["active"] else "▶️ Копировать", f"tt:{a}")],
              [B("Тикет", "noop")] + [B(("✅ " if tk == v else "") + f"${v}", f"tk:{a}:{v}") for v in (5, 10, 15, 25, 50)],
              [B(("✅ " if not tk else "") + "Общий тикет", f"tk:{a}:0"), B("🔗 Найти Solana", f"tl:{a}")],
              [B("🗑 Удалить", f"td:{a}"), B("⬅ Трейдеры", "tr")]]
        return "\n".join(lines), kb

    def wallet(self, u, note=None):
        b = self.b
        addr = b.wallet_address(u)
        live = b.is_live(u)
        if not addr:
            text = ("🟢 Robinhood-кошелёк\n\nСейчас ты в paper-режиме: бот считает сделки по реальным котировкам, но без денег.\n\n"
                    "Для реальных сделок боту нужен свой кошелёк. Ключ хранится на сервере бота — "
                    "держи на нём только рабочую сумму.")
            kb = [[B("🔑 Создать кошелёк", "wc")], [B("⬅ Кошельки", "w")]]
        else:
            usdg = b.chain.erc20_balance(b.usdg, addr) / 1e6
            eth = b.chain.eth_balance(addr) / 1e18
            text = (f"🟢 Robinhood-кошелёк · {'🔴 LIVE' if live else '🟡 PAPER'}\n\n"
                    f"Адрес (Robinhood Chain):\n{addr}\n\n"
                    f"USDG: {usdg:.2f}\nETH: {eth:.5f}\n\n"
                    f"Пополнение: USDG строго {b.usdg} (другие «USDG» — фейки) и немного ETH на газ. "
                    "Удобнее всего через relay.link.")
            kb = [[B("💸 Вывести USDG", "wd:usdg"), B("💸 Вывести ETH", "wd:eth")],
                  [B("🔐 Показать ключ", "wk"), U("🔎 Эксплорер", f"{EXPLORER}/address/{addr}")],
                  [B("🟡 Вернуть PAPER", "live:off") if live else B("🔴 Включить LIVE", "live:on")],
                  [B("🔄 Обновить", "wr"), B("⬅ Кошельки", "w")]]
        if note:
            text += "\n\n" + note
        return text, kb

    def wallets(self, u):
        b = self.b
        rh, sol = b.wallet_address(u), b.sol_address(u)
        text = ("👛 Кошельки\n\n"
                f"🟢 Robinhood Chain · {'🔴 LIVE' if b.live_on(u, 'rh') else '🟡 PAPER'}\n"
                f"🔷 Ethereum · {'🔴 LIVE' if b.live_on(u, 'eth') else '🟡 PAPER'}\n"
                f"{rh or 'EVM-кошелька пока нет'}  (один адрес на обе сети)\n\n"
                f"🟣 Solana · {'🔴 LIVE' if b.live_on(u, 'sol') else '🟡 PAPER'}\n"
                f"{sol or 'кошелька пока нет'}\n\n"
                "Paper-режим общий для обеих сетей: один банк, реальные котировки, без денег. "
                "LIVE включается в каждой сети отдельно.")
        return text, [[B("🟢 Robinhood", "wr"), B("🔷 Ethereum", "weth"), B("🟣 Solana", "wsl")], [B("⬅ Меню", "m")]]

    def wallet_eth(self, u, note=None):
        b = self.b
        addr, live = b.wallet_address(u), b.live_on(u, "eth")
        net = b.nets.get("eth")
        if not net:
            return "Ethereum выключен в config.yaml (chains.ethereum: false).", [[B("⬅ Кошельки", "w")]]
        if not addr:
            text = ("🔷 Ethereum-кошелёк\n\nВ Ethereum бот использует тот же EVM-кошелёк, что и в Robinhood Chain: "
                    "один адрес и один ключ, но балансы в каждой сети свои.")
            kb = [[B("🔑 Создать EVM-кошелёк", "wc")], [B("⬅ Кошельки", "w")]]
        else:
            usdc = net.chain.erc20_balance(net.usd, addr) / 1e6
            eth = net.chain.eth_balance(addr) / 1e18
            text = (f"🔷 Ethereum-кошелёк · {'🔴 LIVE' if live else '🟡 PAPER'}\n\n"
                    f"Адрес (тот же, что в Robinhood):\n{addr}\n\n"
                    f"USDC: {usdc:.2f}\nETH: {eth:.5f}\n\n"
                    "Пополнение: USDC в сети Ethereum (0xA0b8…eB48) на сделки и ETH на газ — от 0.002 ETH. "
                    "С биржи выводи, выбрав сеть Ethereum (ERC-20).")
            kb = [[B("💸 Вывести USDC", "wd:eth_usdc"), B("💸 Вывести ETH", "wd:eth_eth")],
                  [B("🔐 Показать ключ", "wk"), U("🔎 Etherscan", f"{ETHERSCAN}/address/{addr}")],
                  [B("🟡 Вернуть PAPER", "elive:off") if live else B("🔴 Включить LIVE", "elive:on")],
                  [B("🔄 Обновить", "weth"), B("⬅ Кошельки", "w")]]
        if note:
            text += "\n\n" + note
        return text, kb

    def wallet_sol(self, u, note=None):
        b = self.b
        addr, live = b.sol_address(u), b.live_on(u, "sol")
        if not b.sol_rpc:
            return "Solana выключена в config.yaml (chains.solana: false).", [[B("⬅ Кошельки", "w")]]
        if not addr:
            text = ("🟣 Solana-кошелёк\n\nДля реальных сделок в Solana боту нужен свой кошелёк. Ключ хранится на сервере "
                    "бота — держи на нём только рабочую сумму.")
            kb = [[B("🔑 Создать Solana-кошелёк", "wsc")], [B("⬅ Кошельки", "w")]]
        else:
            usdc = b.sol_rpc.token_balance(addr, SOL_USDC) / 1e6
            sol = b.sol_rpc.sol_balance(addr) / 1e9
            text = (f"🟣 Solana-кошелёк · {'🔴 LIVE' if live else '🟡 PAPER'}\n\n"
                    f"Адрес:\n{addr}\n\n"
                    f"USDC: {usdc:.2f}\nSOL: {sol:.4f}\n\n"
                    f"Пополнение: USDC в сети Solana (токен {SOL_USDC}) на сделки и около 0.1 SOL на комиссии и залоги. "
                    "С биржи выводи, выбрав сеть Solana.")
            kb = [[B("💸 Вывести USDC", "wsd:sol_usdc"), B("💸 Вывести SOL", "wsd:sol")],
                  [B("🔐 Показать ключ", "wsk"), U("🔎 Solscan", f"{SOLSCAN}/account/{addr}")],
                  [B("🟡 Вернуть PAPER", "slive:off") if live else B("🔴 Включить LIVE", "slive:on")],
                  [B("🔄 Обновить", "wsl"), B("⬅ Кошельки", "w")]]
        if note:
            text += "\n\n" + note
        return text, kb

    def admin(self, note=None):
        b, db = self.b, self.b.db
        lines = ["👤 Пользователи бота\n"]
        kb = []
        for x in db.users(active_only=False):
            st = db.stats(x["id"])
            pnl = sum((s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) for s in st)
            lines.append(f"{'✅' if x['active'] else '🚫'} {x['name']}{' (владелец)' if x['id'] == 1 else ''} · "
                         f"RH {'LIVE' if b.live_on(x, 'rh') else 'paper'} / ETH {'LIVE' if b.live_on(x, 'eth') else 'paper'}"
                         f" / SOL {'LIVE' if b.live_on(x, 'sol') else 'paper'} · "
                         f"трейдеров {len(db.traders(x['id'], True))} · "
                         f"открыто {len(db.positions(x['id']))} · PnL {usd(pnl)}")
            if x["id"] != 1 and x["active"]:
                kb.append([B(f"🚫 Отключить {x['name']}", f"ud:{x['id']}")])
        if note:
            lines += ["", note]
        kb.insert(0, [B("➕ Пригласить", "inv")])
        kb.append([B("⬅ Меню", "m")])
        return "\n".join(lines), kb

    def settings(self, u):
        b = self.b
        cfg = b.ucfg(u)
        sz, g = cfg["sizing"], cfg["gates"]
        text = ("⚙️ Настройки\n\n"
                f"💵 Размер: тикет {usd(sz['ticket_usd'])} · до {sz['max_positions']} позиций · "
                f"≤{float(sz['max_bank_fraction']) * 100:.0f}% банка на позицию\n\n"
                f"🎯 Выходы: {self.exits_summary(u)}\n\n"
                f"🛡 Фильтры: догон ≤{g['max_chase_pct']}% · пул ≥{big(g['min_liquidity_usd'])} · "
                f"защита {g.get('relay_gate', 'strict')}\n\n"
                f"🔔 Пропущенные сигналы: {'присылаю' if u['notify_skips'] else 'не присылаю'}")
        kb = [[B("💵 Размер", "s:size"), B("🎯 Выходы", "s:exit")],
              [B("🛡 Фильтры", "s:filt"), B("🔔 Уведомления", "s:notif")],
              [B("🌐 Сети", "s:net"), B("⬅ Меню", "m")]]
        return text, kb

    def exits_summary(self, u):
        ex = self.b.ucfg(u)["exits"]
        mode = ex.get("mode", "ladder")
        parts = []
        if mode in ("ladder", "both"):
            parts.append("лесенка " + ladder_str(ex.get("ladder")))
        if mode in ("timed", "both"):
            parts.append("таймер " + timed_str(ex.get("timed")))
        parts.append(f"стоп −{ex['stop_loss_pct']}%" if ex.get("stop_loss_pct") else "без стопа")
        parts.append("после TP в безубыток" if ex.get("stop_after_tp") == "breakeven" else "после TP стоп прежний")
        if ex.get("follow_origin_exit", True):
            parts.append("выход вслед за трейдером")
        return " · ".join(parts)

    def section(self, u, name):
        b = self.b
        cfg = b.ucfg(u)
        back = [B("⬅ Настройки", "set")]
        if name == "size":
            sz = cfg["sizing"]
            text = ("💵 Размер позиции\n\n"
                    f"Тикет: {usd(sz['ticket_usd'])} на одну копию. Он автоматически урезается до "
                    f"{float(sz['max_bank_fraction']) * 100:.0f}% капитала и до 0.25% ликвидности пула.\n"
                    f"Максимум открытых позиций: {sz['max_positions']}.")
            kb = [self._opts(u, "Тикет", "sizing.ticket_usd", [5, 10, 15, 25, 50], lambda v: f"${v}"),
                  [B("✏️ Свой тикет", "c:sizing.ticket_usd")],
                  self._opts(u, "Позиций", "sizing.max_positions", [3, 5, 10, 20], str),
                  self._opts(u, "% банка", "sizing.max_bank_fraction", [0.02, 0.04, 0.1], lambda v: f"{v * 100:.0f}%"),
                  back]
            return text, kb
        if name == "exit":
            ex = cfg["exits"]
            text = ("🎯 Выходы\n\n"
                    f"Сейчас: {self.exits_summary(u)}\n\n"
                    "Лесенка — продаёт долю позиции, когда цена выросла на заданный %.\n"
                    "Таймер — продаёт доли через заданное время (по замерам QorbQuant пик копии в среднем через 5–7 мин).\n"
                    "«Оба» — работают оба правила, что сработает раньше.\n"
                    "Доли считаются от исходной позиции.")
            follow = ex.get("follow_origin_exit", True)
            kb = [self._opts(u, "Режим", "exits.mode", ["ladder", "timed", "both"],
                             lambda v: {"ladder": "Лесенка", "timed": "Таймер", "both": "Оба"}[v])]
            kb += [[B(("✅ " if ex.get("ladder") == v[1] else "") + "📶 " + v[0], f"lad:{k}")] for k, v in LADDERS.items()]
            kb += [[B("✏️ Своя лесенка", "c:exits.ladder")]]
            kb += [[B(("✅ " if ex.get("timed") == v[1] else "") + "⏱ " + v[0], f"tim:{k}")] for k, v in TIMED.items()]
            kb += [[B("✏️ Свой таймер", "c:exits.timed")],
                   self._opts(u, "Стоп", "exits.stop_loss_pct", [25, 40, 50, 0], lambda v: f"−{v}%" if v else "нет"),
                   self._opts(u, "После TP", "exits.stop_after_tp", ["keep", "breakeven"],
                              lambda v: "стоп прежний" if v == "keep" else "безубыток"),
                   [B(("✅" if follow else "❌") + " Выходить вслед за трейдером", "tog:exits.follow_origin_exit")],
                   self._opts(u, "Держать макс.", "exits.max_hold_hours", [6, 24, 72], lambda v: f"{v} ч"),
                   back]
            return text, kb
        if name == "filt":
            g = cfg["gates"]
            text = ("🛡 Фильтры сигналов\n\n"
                    "Догон — насколько наша цена может быть выше цены трейдера.\n"
                    "Пул — минимальная ликвидность.\n"
                    "Покупка от — мелкие пробные покупки трейдера не копируются.\n"
                    "Возраст пула — совсем свежие пулы пропускаются.\n"
                    "Влияние — максимальное влияние нашей сделки на цену.\n"
                    "Защита от подставных: strict — только покупки, подписанные Solana-кошельком трейдера; "
                    "fomo — любые покупки через FOMO; выкл — без проверки.\n\n"
                    f"Сейчас: догон ≤{g['max_chase_pct']}% · пул ≥{big(g['min_liquidity_usd'])} · "
                    f"покупка от {usd(g['min_origin_usd'])} · защита {g.get('relay_gate')}")
            kb = [self._opts(u, "Догон", "gates.max_chase_pct", [10, 25, 50], lambda v: f"≤{v}%"),
                  self._opts(u, "Пул", "gates.min_liquidity_usd", [5000, 10000, 25000], lambda v: f"${v // 1000}k"),
                  self._opts(u, "Покупка от", "gates.min_origin_usd", [20, 50, 200], lambda v: f"${v}"),
                  self._opts(u, "Возраст", "gates.min_pool_age_minutes", [0, 2, 10], lambda v: f"{v} мин"),
                  self._opts(u, "Влияние", "gates.max_price_impact_pct", [3, 5, 10], lambda v: f"≤{v}%"),
                  self._opts(u, "Защита", "gates.relay_gate", ["strict", "fomo", "off"],
                             lambda v: {"strict": "strict", "fomo": "fomo", "off": "выкл"}[v]),
                  back]
            return text, kb
        if name == "net":
            ch = cfg.get("chains") or {}
            rh, sol, eth = ch.get("robinhood", True), ch.get("solana", True), ch.get("ethereum", True)
            text = ("🌐 Сети\n\nВ каких сетях копировать сделки твоих трейдеров. Трейдеры FOMO торгуют во всех трёх: "
                    "в Robinhood Chain и Ethereum бот следит за их EVM-кошельком (адрес один), в Solana — за Solana-кошельком.\n"
                    "Выключенная сеть не трогает уже открытые позиции — они доиграют по выходам.")
            return text, [[B(("✅" if rh else "❌") + " Robinhood Chain", "tog:chains.robinhood")],
                          [B(("✅" if eth else "❌") + " Ethereum", "tog:chains.ethereum")],
                          [B(("✅" if sol else "❌") + " Solana", "tog:chains.solana")], back]
        if name == "notif":
            on = u["notify_skips"]
            muted = json.loads(u.get("muted") or "[]")
            text = ("🔔 Уведомления\n\n"
                    "О покупках и продажах бот пишет всегда.\n"
                    "Пропущенные сигналы — как в copyfomo: сообщение на каждый сигнал, который отсеял фильтр, с причиной "
                    "и цифрами. Раздачи токенов (спам в кошельки трейдеров) не присылаю никогда.\n"
                    "Под каждым пропуском есть кнопка «🔕 Не присылать такие» — она выключает одну причину.")
            if muted:
                text += "\n\nНе присылаю: " + ", ".join(SKIP_RU.get(m, m) for m in muted)
            kb = [[B(("✅" if on else "❌") + " Присылать пропущенные сигналы", "skips")]]
            kb += [[B(f"🔔 Снова присылать: {SKIP_RU.get(m, m)}"[:60], f"unmute:{m}")] for m in muted]
            return text, kb + [back]
        return self.settings(u)

    def stats(self, u):
        db = self.b.db
        st = db.stats(u["id"])
        lines = ["📈 Статистика\n"]
        if st:
            pnl = sum((s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) for s in st)
            wins = sum(1 for s in st if (s["realized_usd"] or 0) - s["cost_usd"] - (s["gas_usd"] or 0) > 0)
            gas = sum(s["gas_usd"] or 0 for s in st)
            lines.append(f"Сделок: {len(st)} · win {wins / len(st) * 100:.0f}% · PnL {usd(pnl)} · газ {usd(gas)}\n")
            for t in db.traders(u["id"]):
                n, wn, p = db.wallet_stats(u["id"], t["address"])
                if n:
                    lines.append(f"{t['label']}: {n} · win {wn / n * 100:.0f}% · {usd(p)}")
        else:
            lines.append("Закрытых сделок пока нет.")
        reasons = db.skip_reasons(u["id"])
        if reasons:
            lines.append("\nПочему пропускались сигналы:")
            lines += [f"· {SKIP_RU.get(r, r)} — {n}" for r, n in reasons]
        return "\n".join(lines), [[B("⏭ Пропущенные монеты", "miss")], [B("📜 Закрытые", "cl"), B("⬅ Меню", "m")]]

    def missed(self, u):
        """Last skipped signals and what the coin did since: shows whether the filters cost or saved money."""
        b = self.b
        rows = b.db.skipped(u["id"], 12, exclude=NOISE | {"already_holding"})  # holding = not a missed coin
        for r in rows:  # older records did not store the network: infer it from the address format
            r["details"].setdefault("chain", "rh" if r["token"].startswith("0x") else "sol")
        if not rows:
            return "⏭ Пропущенных сигналов пока нет.", [[B("⬅ Статистика", "st")]]
        now = {}
        for ch, net in b.nets.items():
            toks = list({r["token"] for r in rows if r["details"]["chain"] == ch and r["details"].get("price")})
            if toks:
                now.update(net.market.prices(toks))
        sol = list({r["token"] for r in rows if r["details"]["chain"] == "sol" and r["details"].get("price")})
        if sol:
            now.update(b.market_sol.prices(sol))
        lines, moves = ["⏭ Пропущенные монеты — что с ними стало\n"], []
        for r in rows:
            d = r["details"]
            ch = d["chain"]
            key = r["token"] if ch == "sol" else r["token"].lower()
            cur, then = now.get(key), d.get("price")
            if cur and then:
                mv = (cur / then - 1) * 100
                moves.append(mv)
                tail = f"{'⚪' if abs(mv) < 1 else '🟢' if mv > 0 else '🔴'} {mv:+.0f}%"
            else:
                tail = "цена неизвестна"
            ago = age((time.time() - r["ts"]) / 60)
            lines.append(f"{r['symbol']} ({TAG.get(ch, ch)}) · {r['label']} · {SKIP_RU.get(r['reason'], r['reason'])}"
                         f" · {ago} назад → {tail}")
        if moves:
            up = sum(1 for m in moves if m > 0)
            lines.append(f"\nС момента пропуска выросли {up} из {len(moves)}, в среднем {sum(moves) / len(moves):+.0f}% "
                         "(по текущей цене, без комиссий и выходов — грубая оценка).")
        return "\n".join(lines), [[B("🔄 Обновить", "miss"), B("⬅ Статистика", "st")]]

    def closed(self, u):
        rows = self.b.db.closed_positions(u["id"], 12)
        if not rows:
            return "📜 Закрытых сделок пока нет.", [[B("⬅ Меню", "m")]]
        lines = ["📜 Последние закрытые:\n"]
        for p in rows:
            pnl = (p["realized_usd"] or 0) - p["cost_usd"] - (p["gas_usd"] or 0)
            icon = "✖" if p["status"] == "writeoff" else ("🟢" if pnl >= 0 else "🔴")
            lines.append(f"{icon} {p['symbol']} · {usd(pnl)} ({pnl / p['cost_usd'] * 100:+.0f}%) · {p['label']}")
        return "\n".join(lines), [[B("📈 Статистика", "st"), B("⬅ Меню", "m")]]
