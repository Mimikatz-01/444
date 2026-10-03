"""Forced-facts integration: the whole entry -> position -> exit pipeline for every chain.

`facts` / `facts_sol` are monkeypatched to fixed facts for a liquid token (no detection RPC), and
the quote providers (Relay, Jupiter) and the market price feed are stubbed with deterministic
numbers so the test is fully offline. Everything else runs for real: judge's gates, sizing, the
paper executor, the position row, cash accounting, notifications and strategy.exit_plan.

This is the regression guard the task (Этап 0) asks for: it must pass before and after the feature
work. A real-quote variant is covered by the @network UI/relay smoke tests."""
import json
import time

import pytest

from conftest import make_bot
from rhcopy.relay import Quote

WALLET = "0xddd462bb053b57d5d73c9615e11a7284cfee9233"
SOLW = "DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6"
RH_TOKEN = "0x9e795209be691654fb908ab86b3a651551939e16"
ETH_TOKEN = "0x6982508145454ce325ddbe47a25d4ec3d2311933"
BSC_TOKEN = "0x1111111111111111111111111111111111110056"
BASE_TOKEN = "0x2222222222222222222222222222222222228453"
SOL_TOKEN = "5GDX5fJTQns4arM1J94wjxdA8KXFsV5qW46MLFpHpump"
PRICE = 0.001


def evm_facts(chain, dec=18):
    return {"chain": chain, "age_s": 5.0, "own": True, "paid_eth": False, "stock": False, "rec": None,
            "pair": set(), "tx_from": WALLET, "tx_to": "", "dec": dec, "amount": 50000.0, "origin_usd": 50.0,
            "info": {"price_usd": PRICE, "liquidity_usd": 50000.0, "mcap_usd": 200000.0,
                     "pair_created_ms": int((time.time() - 3600) * 1000), "buys_h24": 10, "sells_h24": 5}}


def sol_facts(dec=6):
    return {"chain": "sol", "age_s": 4.0, "own": True, "paid_eth": False, "stock": False, "rec": None,
            "pair": set(), "tx_from": SOLW, "tx_to": "", "dec": dec, "amount": 50000.0, "origin_usd": 50.0,
            "info": {"price_usd": PRICE, "liquidity_usd": 50000.0, "mcap_usd": 200000.0,
                     "pair_created_ms": int((time.time() - 3600) * 1000), "buys_h24": 10, "sells_h24": 5}}


def relay_quote_stub(token, usd_dec=6):
    """Deterministic same-chain quote: buy stable->token at PRICE, sell token->stable at 0.99*PRICE."""
    def q(user, cin, cout, amount_raw, slippage_bps=None, chain_id=None):
        if cout.lower() == token.lower():  # stable -> token (buy)
            usd = amount_raw / 10 ** usd_dec
            out = int(usd / PRICE * 10 ** 18)
            return Quote("r", out, out, usd, usd, -1.0, 0.05, 0.0, [])
        tokens = amount_raw / 10 ** 18  # token -> stable (sell / round-trip back)
        out = int(tokens * PRICE * 0.99 * 10 ** usd_dec)
        return Quote("r", out, out, 0.0, 0.0, -1.0, 0.05, 0.0, [])
    return q


def jup_order_stub(mint, dec=6):
    def order(input_mint, output_mint, amount, taker=None):
        if output_mint == mint:  # USDC -> token (buy)
            usd = amount / 1e6
            return {"outAmount": str(int(usd / PRICE * 10 ** dec)), "priceImpact": 0.4}
        tokens = amount / 10 ** dec  # token -> USDC (sell)
        return {"outAmount": str(int(tokens * PRICE * 0.99 * 1e6)), "priceImpact": 0.4}
    return order


def stub_prices(bot, monkeypatch, mapping):
    """Make every market feed return a fixed price map (used by update_positions / exits)."""
    def prices_for(market):
        return lambda tokens: {t: mapping[t] for t in tokens if t in mapping}
    for net in bot.nets.values():
        monkeypatch.setattr(net.market, "prices", prices_for(net.market))
    monkeypatch.setattr(bot.market_sol, "prices", prices_for(bot.market_sol))


@pytest.fixture
def ff_bot(tmp_path, monkeypatch):
    bot = make_bot(tmp_path / "bot.db")
    bot.db.update_user(1, chat_id="1001", name="owner")
    bot.db.add_trader(1, WALLET, "alpha")
    bot.db.add_trader(1, SOLW, "solguy")
    bot.db.set_pair(SOLW, SOLW)
    # symbols without network
    for net in bot.nets.values():
        net.chain._sym[RH_TOKEN] = "RHT"
        net.chain._sym[ETH_TOKEN] = "ETHT"
    monkeypatch.setattr(bot, "sol_price", lambda: 150.0)
    monkeypatch.setattr(bot.market_sol, "info", lambda mint, fresh=False: {"symbol": "SOLT", "price_usd": PRICE})
    return bot


@pytest.mark.parametrize("chain,token,usd_dec", [("rh", RH_TOKEN, 6), ("eth", ETH_TOKEN, 6),
                                                 ("bsc", BSC_TOKEN, 18), ("base", BASE_TOKEN, 6)])
def test_evm_buy_and_exit(ff_bot, monkeypatch, chain, token, usd_dec):
    bot = ff_bot
    assert bot.nets[chain].usd_decimals == usd_dec  # BNB Chain stablecoin has 18 decimals, others 6
    bot.nets[chain].chain._sym[token] = "TT"
    monkeypatch.setattr(bot, "facts", lambda *a, **k: evm_facts(chain))
    monkeypatch.setattr(bot.relay, "quote", relay_quote_stub(token, usd_dec))
    cash0 = bot.cash(bot.db.user(1), chain)

    bot.on_signal(WALLET, token, int(50000 * 1e18), 1000, "0xtx", bot.nets[chain])

    pos = bot.db.positions(1)
    assert len(pos) == 1 and pos[0]["token"] == token and (pos[0]["chain"] or "rh") == chain
    p = pos[0]
    assert abs(p["cost_usd"] - 15.0) < 1e-6  # ticket is $15 regardless of the stablecoin's decimals
    assert abs(bot.cash(bot.db.user(1), chain) - (cash0 - 15.0)) < 1e-6  # paper cash debited
    assert any("КУПИЛ" in t for t in bot.tg.texts("1001"))

    # exit: price collapses below the stop-loss -> sell everything, position closes, cash returns
    stub_prices(bot, monkeypatch, {token: PRICE * 0.4})
    bot._last_prices = 0
    bot.update_positions()
    p = bot.db.position_by_id(p["id"])
    assert p["status"] == "closed" and p["tokens_left"] == 0
    assert bot.cash(bot.db.user(1), chain) > cash0 - 15.0  # got some money back
    assert any("ПРОДАЛ" in t for t in bot.tg.texts("1001"))


def test_sol_buy_and_exit(ff_bot, monkeypatch):
    bot = ff_bot
    monkeypatch.setattr(bot, "facts_sol", lambda *a, **k: sol_facts())
    monkeypatch.setattr(bot.jup, "order", jup_order_stub(SOL_TOKEN))
    cash0 = bot.cash(bot.db.user(1), "sol")

    ev = {"mint": SOL_TOKEN, "pre": 0, "post": int(50000 * 1e6), "dec": 6}
    tr = {"signer": True, "block_time": time.time(), "slot": 123, "sol_delta": -10**8, "usdc_delta": 0, "tokens": [ev]}
    bot.on_signal_sol(SOLW, SOLW, ev, tr, "solsig")

    pos = bot.db.positions(1)
    assert len(pos) == 1 and pos[0]["chain"] == "sol" and pos[0]["token"] == SOL_TOKEN
    p = pos[0]
    assert abs(p["cost_usd"] - 15.0) < 1e-6
    assert abs(bot.cash(bot.db.user(1), "sol") - (cash0 - 15.0)) < 1e-6
    assert any("КУПИЛ" in t for t in bot.tg.texts("1001"))

    stub_prices(bot, monkeypatch, {SOL_TOKEN: PRICE * 0.4})
    bot._last_prices = 0
    bot.update_positions()
    p = bot.db.position_by_id(p["id"])
    assert p["status"] == "closed" and p["tokens_left"] == 0
    assert bot.cash(bot.db.user(1), "sol") > cash0 - 15.0
    assert any("ПРОДАЛ" in t for t in bot.tg.texts("1001"))
