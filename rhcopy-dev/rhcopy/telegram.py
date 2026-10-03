"""Telegram transport for several users. Runs in its own threads and never touches trading state:
every update (message or button press, from any chat) goes to `inbox`; the main loop decides who
is allowed. Everything outgoing goes through `outbox` and is sent from here."""
import queue
import threading
import time

import requests


class Telegram:
    def __init__(self, token, log=None):
        self.api = f"https://api.telegram.org/bot{token}"
        self.log = log
        self.inbox, self.outbox = queue.Queue(), queue.Queue()
        self.offset = 0
        self.s = requests.Session()

    def start(self):
        threading.Thread(target=self._poll, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()
        self.outbox.put(("setMyCommands", {"commands": [
            {"command": "menu", "description": "Главное меню"},
            {"command": "help", "description": "Команды"}]}, None))

    # ---------- outgoing (thread-safe: only queue puts) ----------
    def send(self, chat, text, kb=None, reply_kb=None, extra=None, delete_after=None):
        if not chat:
            return
        p = {"chat_id": chat, "text": text, "disable_web_page_preview": True, **(extra or {})}
        if kb is not None:
            p["reply_markup"] = {"inline_keyboard": kb}
        elif reply_kb is not None:
            p["reply_markup"] = reply_kb
        self.outbox.put(("sendMessage", p, delete_after))

    def edit(self, chat, msg_id, text, kb=None):
        self.outbox.put(("editMessageText", {"chat_id": chat, "message_id": msg_id, "text": text,
                                             "disable_web_page_preview": True,
                                             "reply_markup": {"inline_keyboard": kb or []}}, None))

    def answer(self, cb_id, text=None):
        p = {"callback_query_id": cb_id}
        if text:
            p["text"] = text
        self.outbox.put(("answerCallbackQuery", p, None))

    def _send_loop(self):
        while True:
            method, p, delete_after = self.outbox.get()
            payloads = [p]
            if method == "sendMessage" and len(p["text"]) > 3900:  # split long text, keyboard on the last part
                parts = [p["text"][i:i + 3900] for i in range(0, len(p["text"]), 3900)]
                payloads = [{**p, "text": t} for t in parts]
                for x in payloads[:-1]:
                    x.pop("reply_markup", None)
            for x in payloads:
                try:
                    r = self.s.post(f"{self.api}/{method}", json=x, timeout=15).json()
                    wait = (r.get("parameters") or {}).get("retry_after")
                    if wait:  # flood control: wait as told, then send once more
                        time.sleep(float(wait) + 0.5)
                        r = self.s.post(f"{self.api}/{method}", json=x, timeout=15).json()
                    if not r.get("ok") and "not modified" not in str(r.get("description", "")) and self.log:
                        self.log.warning("telegram %s: %s", method, r.get("description"))
                    if delete_after and r.get("ok"):
                        mid = r["result"]["message_id"]
                        threading.Timer(delete_after, self.outbox.put, args=(
                            ("deleteMessage", {"chat_id": x["chat_id"], "message_id": mid}, None),)).start()
                except Exception as e:
                    if self.log:
                        self.log.warning("telegram %s failed: %s", method, e)
                time.sleep(0.04)

    # ---------- incoming ----------
    def _poll(self):
        while True:
            try:
                r = self.s.get(f"{self.api}/getUpdates", timeout=40, params={
                    "timeout": 30, "offset": self.offset,
                    "allowed_updates": '["message","callback_query"]'}).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1
                    if "callback_query" in u:
                        cq = u["callback_query"]
                        msg = cq.get("message") or {}
                        self.inbox.put({"kind": "cb", "chat": str((msg.get("chat") or {}).get("id", "")),
                                        "data": cq.get("data", ""), "msg_id": msg.get("message_id"),
                                        "cb_id": cq["id"], "name": self._name(cq.get("from"))})
                    elif "message" in u:
                        m = u["message"]
                        text = (m.get("text") or "").strip()
                        if text:
                            self.inbox.put({"kind": "text", "chat": str((m.get("chat") or {}).get("id", "")),
                                            "text": text, "name": self._name(m.get("from"))})
            except Exception as e:
                if self.log:
                    self.log.warning("telegram poll error: %s", e)
                time.sleep(5)

    @staticmethod
    def _name(frm):
        frm = frm or {}
        return frm.get("username") or " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "user"
