"""Button UI: crawl every screen reachable from the main menu for two users, plus the multi-user guards."""
from conftest import EVM1, SOL_ONLY, seed

# destructive or confirming actions are never pressed by the crawler (see AGENTS.md)
SKIP = {"tdc", "udc", "wc", "wkc", "wsc", "wskc", "lvc", "wdc", "inv", "impc"}


def buttons(kb):
    for row in kb or []:
        for btn in row:
            yield btn


def crawl(bot, uid, start="m"):
    seen, todo = set(), [start]
    while todo:
        data = todo.pop(0)
        if data in seen:
            continue
        seen.add(data)
        u = bot.db.user(uid)  # every action re-reads the user, like handle_inbox does
        screen, _toast = bot.ui.route(u, data)
        if not screen:
            continue
        text, kb = screen
        assert isinstance(text, str) and text.strip(), data
        for btn in buttons(kb):
            cb = btn.get("callback_data")
            if cb is None:
                assert btn.get("url", "").startswith("https://"), btn
                continue
            assert len(cb.encode("utf-8")) <= 64, cb
            if cb.split(":")[0] not in SKIP and cb not in seen:
                todo.append(cb)
    return seen


def test_crawl_two_users(bot):
    uid2 = seed(bot)
    for uid in (1, uid2):
        seen = crawl(bot, uid)
        assert {"m", "pos", "tr", "set", "st", "w"} <= seen
        assert len(seen) > 40
    seen_admin = crawl(bot, 1, "adm")
    assert "adm" in seen_admin


def test_inbox_path_edits_message(bot):
    uid2 = seed(bot)
    chat = bot.db.user(uid2)["chat_id"]
    bot.tg.inbox.put({"kind": "cb", "chat": chat, "data": "st", "msg_id": 7, "cb_id": "q1", "name": "friend"})
    bot.tg.inbox.put({"kind": "text", "chat": chat, "text": "/menu", "name": "friend"})
    bot.handle_inbox()
    assert bot.tg.answers and bot.tg.answers[0]["cb_id"] == "q1"
    assert bot.tg.edits and bot.tg.edits[0]["msg_id"] == 7 and "Статистика" in bot.tg.edits[0]["text"]
    assert any("rhcopy" in t for t in bot.tg.texts(chat))


def test_stranger_gets_hint_not_access(bot):
    seed(bot)
    bot.tg.inbox.put({"kind": "text", "chat": "999", "text": "/menu", "name": "x"})
    bot.handle_inbox()
    assert bot.tg.texts("999") and "приватный" in bot.tg.texts("999")[0]


def test_text_flows(bot):
    uid2 = seed(bot)
    u = bot.db.user(uid2)
    bot.ui.route(u, "tadd")
    bot.ui.on_text(bot.db.user(uid2), "0x1111111111111111111111111111111111111111 newbie")
    assert bot.db.trader(uid2, "newbie")
    bot.ui.route(bot.db.user(uid2), "c:sizing.ticket_usd")
    bot.ui.on_text(bot.db.user(uid2), "20")
    assert bot.get_key(bot.db.user(uid2), "sizing.ticket_usd") == 20
    bot.ui.route(bot.db.user(uid2), "wd:rh:usd")
    bot.ui.on_text(bot.db.user(uid2), "0x2222222222222222222222222222222222222222 5")
    assert "Кошелька нет" in bot.tg.texts()[-1]


def test_users_cannot_touch_each_other(bot):
    uid2 = seed(bot)
    u2 = bot.db.user(uid2)
    owner_pos = bot.db.positions(1)[0]["id"]
    text, _kb = bot.ui.route(u2, f"p:{owner_pos}")[0]
    assert "не найдена" in text
    bot.ui.route(u2, f"psc:{owner_pos}:100")
    assert not bot.db.position_by_id(owner_pos)["state"].get("manual")
    screen, toast = bot.ui.route(u2, "adm")
    assert toast == "только для владельца бота"
    screen, toast = bot.ui.route(u2, "udc:1")
    assert bot.db.user(1)["active"] == 1
    # trader of user 1 that user 2 does not have
    text, _kb = bot.ui.route(u2, f"t:{SOL_ONLY}")[0]
    assert "не найден" in text
    assert bot.db.trader(1, EVM1)
