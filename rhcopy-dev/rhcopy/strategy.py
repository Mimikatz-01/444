"""Copy strategy: how much to buy and when to sell. Pure functions, no I/O."""


def ticket_size(sizing, wallet_ticket, equity, liquidity_usd):
    """Fixed ticket, capped by bank share and by pool depth."""
    t = float(wallet_ticket or sizing["ticket_usd"])
    if sizing.get("max_bank_fraction"):
        t = min(t, equity * float(sizing["max_bank_fraction"]))
    if liquidity_usd and sizing.get("max_liquidity_fraction"):
        t = min(t, liquidity_usd * float(sizing["max_liquidity_fraction"]))
    return round(t, 2)


def exit_plan(pos, price, now, ex):
    """Decide what to sell now.

    pos: position dict (cost_usd, tokens_initial, tokens_left, decimals, opened, state)
    price: current USD price per whole token (None if unknown)
    ex: the `exits` config section
    Returns (reason, tokens_raw, state_updates) or None.
    Fractions in the ladder and in timed tranches are of the ORIGINAL position.
    """
    st = pos["state"]
    left = pos["tokens_left"]
    if left <= 0:
        return None
    init = pos["tokens_initial"]
    age_min = (now - pos["opened"]) / 60.0

    # 1. hard exits: manual, planted fill, trader exit, max hold
    if st.get("manual"):
        frac = float(st.get("manual_frac") or 1.0)
        return ("manual", left if frac >= 1 else max(1, int(left * frac)), {})
    if st.get("planted"):
        return ("planted", left, {})
    if st.get("mirror_frac") and ex.get("follow_origin_exit", True):
        # exit_style=mirror: sell the same fraction of our bag that the trader just sold
        frac = min(1.0, float(st["mirror_frac"]))
        return ("origin_exit", left if frac >= 0.999 else max(1, int(left * frac)), {"mirror_frac": 0})
    if st.get("origin_exit") and ex.get("follow_origin_exit", True):
        return ("origin_exit", left, {})
    if ex.get("max_hold_hours") and age_min >= float(ex["max_hold_hours"]) * 60:
        return ("max_hold", left, {})

    mult = None
    if price and pos["entry_price"] > 0:
        mult = price / pos["entry_price"]

    # 2. stop loss (breakeven after a take-profit if configured)
    if mult is not None:
        if st.get("any_tp") and ex.get("stop_after_tp", "keep") == "breakeven":
            if mult <= 1.0:
                return ("breakeven", left, {})
        elif ex.get("stop_loss_pct"):
            if mult <= 1 - float(ex["stop_loss_pct"]) / 100.0:
                return ("stop_loss", left, {})

    # 3. take-profit ladder and timed tranches (can fire together)
    mode = ex.get("mode", "ladder")
    frac, reasons, upd = 0.0, [], {}
    if mode in ("ladder", "both") and mult is not None:
        done = set(st.get("tp_done", []))
        for i, rung in enumerate(ex.get("ladder") or []):
            if i not in done and mult >= 1 + float(rung["at_pct"]) / 100.0:
                frac += float(rung["sell"])
                done.add(i)
                reasons.append(f"tp{rung['at_pct']}")
        if reasons:
            upd["tp_done"] = sorted(done)
            upd["any_tp"] = True
    if mode in ("timed", "both"):
        done = set(st.get("timed_done", []))
        fired = False
        for i, tr in enumerate(ex.get("timed") or []):
            if i not in done and age_min >= float(tr["after_min"]):
                frac += float(tr["sell"])
                done.add(i)
                reasons.append(f"t{tr['after_min']}")
                fired = True
        if fired:
            upd["timed_done"] = sorted(done)
            upd["any_tp"] = True
    if frac > 0:
        amt = min(left, int(init * min(frac, 1.0)))
        # schedule already sums to 100% -> sell whatever is left instead of leaving dust
        if left - amt < init * 0.01:
            amt = left
        return ("|".join(reasons), amt, upd)
    return None
