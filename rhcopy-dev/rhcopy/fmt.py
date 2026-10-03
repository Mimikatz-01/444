"""Formatting helpers shared by the bot and the button UI."""


def usd(x):
    x = float(x or 0)
    sign = "-" if x < 0 else ""
    a = abs(x)
    return f"{sign}${a:,.2f}" if a >= 1 else f"{sign}${a:.3f}"


def big(x):
    """Compact dollars for liquidity / market cap: $4.2M, $56k."""
    x = float(x or 0)
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}k"
    return f"${x:.0f}"


def pairs(w):
    """A trader's paired Solana wallets (comma-separated in the DB)."""
    return {x.strip() for x in (w.get("solana") or "").split(",") if x.strip()}


def age(minutes):
    if minutes < 60:
        return f"{minutes:.0f} мин"
    if minutes < 60 * 48:
        return f"{minutes / 60:.1f} ч"
    return f"{minutes / 1440:.0f} дн"


EXIT_RU = {"stop_loss": "стоп-лосс", "breakeven": "безубыток", "origin_exit": "трейдер вышел",
           "max_hold": "время вышло", "manual": "вручную", "planted": "подставная покупка"}


def exit_reason(r):
    out = []
    for x in r.split("|"):
        if x in EXIT_RU:
            out.append(EXIT_RU[x])
        elif x.startswith("tp"):
            out.append(f"TP +{x[2:]}%")
        elif x.startswith("t"):
            out.append(f"таймер {x[1:]} мин")
        else:
            out.append(x)
    return ", ".join(out)


SKIP_RU = {
    "paused": "пауза", "already_holding": "уже держим", "rugged_before": "раньше был раг", "cooldown": "кулдаун",
    "max_positions": "лимит позиций", "stale": "сигнал устарел", "stock_token": "токен-акция", "low_gas": "мало ETH на газ",
    "not_a_swap": "не свап (раздача)", "plant_eth": "оплачено чужим ETH", "relay_unknown": "Relay не ответил",
    "not_fomo_fill": "не сделка FOMO", "no_pair": "нет Solana-пары", "planted": "подставная покупка",
    "no_market_data": "нет данных о токене", "dust_origin": "мелкая покупка трейдера", "low_liquidity": "мало ликвидности",
    "mcap_low": "капа ниже порога", "mcap_high": "капа выше порога", "young_pool": "слишком молодой пул",
    "sell_ratio": "мало продаж (ханипот?)", "ticket_too_small": "тикет слишком мал", "no_cash": "нет кэша",
    "no_route": "нет маршрута покупки", "chase": "цена убежала", "impact": "сильное влияние на цену",
    "gas": "газ дороже лимита", "no_exit_route": "нет маршрута продажи", "round_trip": "большая потеря на круге",
    "not_signed": "не сделка трейдера (токены прислали)", "freezable": "токен можно заморозить",
    "token_ext": "опасные расширения токена", "chain_off": "сеть выключена",
    "not_first_buy": "не первая покупка трейдера", "no_confluence": "нет совпадения",
    "insider_bundle": "скупка пачкой на запуске", "dev_holding": "много у создателя",
    "top10_concentration": "концентрация у топ-держателей", "insider_cluster": "кошельки вокруг создателя",
    "dev_rugger": "создатель уже рагал", "risk_unknown": "риск не проверить",
}

# where a trader was imported from / how a position was entered — for the "По источникам" screen
IMPORT_RU = {"manual": "вручную", "fomo": "FOMO", "gmgn": "GMGN", "kolscan": "kolscan",
             "madeonsol": "MadeOnSol", "paste": "вставка"}
ENTRY_RU = {"wallet": "по кошельку", "confluence": "совпадение", "tg": "канал"}


NOISE = {"not_a_swap", "not_signed"}  # tokens pushed into a trader's wallet: spam, never reported


def skip_detail(reason, d, g):
    """Why a signal was skipped, with the numbers behind it."""
    d, g = d or {}, g or {}
    lim = lambda k: g.get(k)
    if reason == "chase" and "chase" in d:
        return f"цена убежала: наша цена выше цены трейдера на {d['chase']:+.1f}% (лимит {lim('max_chase_pct')}%)"
    if reason == "impact" and "impact" in d:
        return f"наша сделка сдвинула бы цену на {abs(d['impact']):.1f}% (лимит {lim('max_price_impact_pct')}%)"
    if reason == "stale" and "age_s" in d:
        return f"сигнал дошёл через {d['age_s']:.0f} с (лимит {lim('max_signal_age_s')} с)"
    if reason == "token_ext":
        return "у токена опасные расширения: " + ", ".join(d.get("ext") or []) + " (комиссия за перевод / хуки)"
    if reason == "low_liquidity" and "liq" in d:
        return f"ликвидность пула {big(d['liq'])} (минимум {big(lim('min_liquidity_usd'))})"
    if reason == "dust_origin" and "origin_usd" in d:
        return f"трейдер купил всего на {usd(d['origin_usd'])} (порог {usd(lim('min_origin_usd'))})"
    if reason == "young_pool" and "pool_age_min" in d:
        return f"пулу всего {d['pool_age_min']} мин (минимум {lim('min_pool_age_minutes')} мин)"
    if reason == "no_cash" and "ticket" in d:
        return f"не хватает кэша на тикет {usd(d['ticket'])}"
    if reason == "round_trip" and "round_trip_loss" in d:
        return f"купить и сразу продать — потеря {d['round_trip_loss']:.1f}% (лимит {lim('max_round_trip_loss_pct')}%)"
    if reason == "gas" and "gas_pct" in d:
        return f"комиссия сети {d['gas_pct']:.1f}% от тикета (лимит {lim('max_gas_pct')}%)"
    if reason in ("mcap_low", "mcap_high") and "mcap" in d:
        return f"капа {big(d['mcap'])} вне заданных границ"
    if reason == "not_first_buy":
        extra = f" на {usd(d['add_usd'])}" if d.get("add_usd") else ""
        return f"трейдер докупил монету{extra} — это не первый вход, по докупкам не копирую"
    if reason == "no_confluence":
        got, need = d.get("sources", 1), d.get("need", 2)
        return f"за окно купил только {got} из нужных {need} кошельков — совпадения не было"
    if reason == "insider_bundle" and "bundle_pct" in d:
        return f"на запуске пачкой скупили {d['bundle_pct']:.0f}% (лимит {lim('max_bundle_pct')}%)"
    if reason == "dev_holding" and "dev_pct" in d:
        return f"у создателя {d['dev_pct']:.0f}% supply (лимит {lim('max_dev_pct')}%)"
    if reason == "top10_concentration" and "top10_pct" in d:
        return f"у топ-10 держателей {d['top10_pct']:.0f}% (лимит {lim('max_top10_pct')}%)"
    if reason == "insider_cluster" and "insider_pct" in d:
        return f"у кошельков вокруг создателя {d['insider_pct']:.0f}% (лимит {lim('max_insider_pct')}%)"
    if reason == "dev_rugger":
        return "создатель токена уже сливал прошлые запуски"
    if reason == "risk_unknown":
        return "не удалось проверить риск токена, а режим строгий — пропускаю"
    return SKIP_RU.get(reason, reason)
