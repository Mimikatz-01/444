# AGENTS.md — rhcopy

Read this file fully before changing anything. It describes how the project is built and the rules every change must follow.

## What this project is

`rhcopy` is a multi-user copy-trading bot for memecoins, controlled from Telegram with buttons (Russian UI).
- **Networks:** Robinhood Chain (EVM, chain id 4663), Ethereum (chain id 1), Solana.
- **What it does:** it watches the wallets of chosen traders. When a trader enters a new token, the bot runs the signal through filters ("gates"), buys for every user who follows that trader, and then manages exits.
- **Paper mode** is the default: real quotes, simulated fills, nothing is sent on-chain.

Runtime: Python 3.11+, one process, synchronous main loop plus a few background threads that talk to it only through queues.

```
python main.py run    --config config.yaml --db data/bot.db   # the bot
python main.py status --config config.yaml --db data/bot.db   # every user: mode, cash, PnL
python main.py route  <token> [usd]                           # dry-run quote for a Robinhood token
python main.py learnpair <0xaddr|label>                       # find a FOMO trader's paired Solana wallet
```

## Architecture map

| File | Responsibility |
|---|---|
| `main.py` | CLI, `.env` loading (whitelist of keys in `load_env`), logging |
| `rhcopy/bot.py` | `CopyBot`: the main loop and the whole signal pipeline. `EvmNet` describes one EVM network |
| `rhcopy/db.py` | SQLite: users, traders (per user), trader_pairs, signals, positions, fills, kv; idempotent migrations |
| `rhcopy/ui.py` | Button UI: `UI.route(u, data)` dispatches `callback_data`, every screen returns `(text, keyboard)` |
| `rhcopy/telegram.py` | Telegram Bot API transport thread: `inbox` / `outbox` queues, flood-control retry |
| `rhcopy/strategy.py` | Pure functions: `ticket_size`, `exit_plan` (TP ladder, timed tranches, SL, breakeven, origin exit, manual, max hold) |
| `rhcopy/executor.py` | EVM buy/sell via Relay (paper = quote only; live = signed EIP-1559 tx), withdrawals |
| `rhcopy/relay.py` | Relay API: quotes per chain id, fill provenance records, solver/contract lists per chain, `learn_pair` |
| `rhcopy/chain.py` | Minimal EVM JSON-RPC client with primary-first failover, batching, ERC-20 helpers |
| `rhcopy/market.py` | DexScreener per network (`Market("robinhood" / "ethereum" / "solana")`) |
| `rhcopy/sol.py` | Solana: RPC client, `SolWatcher` (WebSocket `logsSubscribe` + polling fallback), `parse_trade`, Jupiter Swap V2 client, `SolExecutor`, SPL helpers |
| `rhcopy/fmt.py` | Formatting, Russian skip-reason texts (`SKIP_RU`, `skip_detail`), `NOISE` reasons that are never reported |

### Signal pipeline (keep this shape)

```
EVM:     scan() -> scan_net(net) -> on_incoming() -> on_signal(net) -> facts(net) -> judge(u) -> buy(u)
Solana:  scan_sol() -> on_sol_tx() -> on_signal_sol() -> facts_sol() -> judge(u) -> buy(u)
Exits:   update_positions() -> strategy.exit_plan() -> do_sell()      (every price_check_seconds)
         recheck_provenance()                                          (every tick, Relay re-check)
Trader sells:  on_outgoing() / on_sol_exit() set state.origin_exit
```

- **`facts`** holds everything about a fill that does not depend on the user. It is computed once per signal.
- **`judge`** applies one user's settings. It raises `Skip(reason, **details)` or returns `(ticket, details)`. Followers are served in rotating order (`self._rr`) so nobody is always second.
- **Every decision is recorded** with `db.signal(...)`: `bought`, `skipped` or `failed`. Skips go through `log_skip`: it stores chain, price, mcap and origin_usd and reports to the user unless the reason is in `NOISE` or muted.

## Invariants — never break these

1. **Paper by default.** Nothing is sent on-chain unless `live_on(u, chain)` is true. New signal sources are paper-only unless the user explicitly enables live for that source.
2. **Exits never stop.** Pause, errors in a signal or a broken source must not stop `update_positions` or `do_sell`. Wrap per-item processing in try/except and log it.
3. **The main loop never blocks for long.** Every network call has a timeout. Long-running or async clients (Telegram bot API, Solana WebSocket, a Telethon client) run in their own thread with their own event loop and hand data to the main loop through a `queue.Queue`. Their callbacks never mutate DB state.
4. **Database migrations are idempotent and additive.** Use `CREATE TABLE IF NOT EXISTS`, and `ALTER TABLE ADD COLUMN` only after checking `PRAGMA table_info`. Never drop or rename columns. A database from any previous version must open and work. One-time data migrations are guarded by a `kv` flag.
5. **Old `config.yaml` files must keep working.** Every new config key gets a default via `setdefault` in `CopyBot.__init__`. Per-user editable sections are listed in `bot.EDITABLE`; new per-user settings live inside one of them.
6. **Addresses.** EVM addresses are lower-cased; Solana addresses are case-sensitive. Use `db.norm()`. Positions carry `chain` (`rh` / `eth` / `sol`).
7. **Multi-user safety.** Every UI action re-reads the user from the DB. It must check that the object (position, trader, channel) belongs to that user — see `UI._own`. Admin-only actions check `uid == 1`.
8. **UI conventions.**
   - All user-facing text is Russian and plain text (no Markdown).
   - Buttons are built with `ui.B(text, data)`; `callback_data` must be at most 64 bytes.
   - A button press edits the same message: return `(screen, toast)` from `route`.
   - Keep the existing tone: short lines, emoji markers, numbers with limits ("догон +31% (лимит 25%)").
9. **Secrets.** Keys live only in `.env` (add new names to the whitelist in `main.load_env`). Never log, print or send keys. Session files must be created with mode 600.
10. **Rate limits are real.**
    - Jupiter without a key allows 0.5 req/s (the `Jupiter` class throttles).
    - Public RPCs throttle.
    - MadeOnSol's free tier is 200 requests per day.

    Cache aggressively and never call an external API inside a tight loop.

## How to test

There is no live Telegram token and no funded wallet in development. Test like this:
- **Fake Telegram.** Build `CopyBot` with `cfg["telegram"]["enabled"] = False`, then attach a fake transport that records `send`/`edit`/`answer` calls and a real `UI(bot)`. Drive it with `bot.tg.inbox.put({...})` + `bot.handle_inbox()`, or call `bot.ui.route(user, data)` directly.
- **UI crawl.** For each test user, do a BFS over every `callback_data` reachable from `"m"`. Skip destructive or confirming actions (`tdc`, `udc`, `wc`, `wkc`, `wsc`, `wskc`, `livec`, `slivec`, `elivec`, `wdc`, `inv` and any new confirm actions). Assert there are no exceptions and every `callback_data` is at most 64 bytes.
- **Migration test.** Copy a database created by the previous version, open it with the new code, check that the new columns and tables exist and `status_text` works.
- **Forced-facts integration.** Monkeypatch `bot.facts` / `bot.facts_sol` to return fixed facts for a real liquid token. Call `on_signal` / `on_signal_sol` and assert positions, cash, notifications and exits. Paper mode, real quotes.
- **Live read-only run.** `timeout 120 python main.py run --config <test cfg> --db <test db>` and check the logs for errors. Never point tests at the production database `/opt/rhcopy/data/bot.db`.

Put automated tests in `tests/` (pytest; add `pytest` to `requirements-dev.txt` only). Network-dependent tests must be marked and skippable (`@pytest.mark.network`).

## Working rules for the agent

- Work in the phases of the task file. After each phase, run all tests and make one git commit with a clear message.
- Read the code paths you are about to change before changing them. Keep diffs focused; do not refactor unrelated code.
- Never enable live mode, never send transactions, never use real private keys in tests.
- Do not invent third-party API endpoints. Read the official docs. If they are unreachable, implement the integration behind a clear "not configured / unavailable" state and say so in the report.
- Do not scrape sites that protect themselves against bots (GMGN, kolscan): use paste/URL import instead.
- Update `README.md` (Russian), `config.example.yaml`, `.env.example` and `CHANGELOG.md` with every user-visible change.
- Finish with a report: what changed, how it was tested (commands and results), known limitations, and which keys or one-time steps the human must do.
