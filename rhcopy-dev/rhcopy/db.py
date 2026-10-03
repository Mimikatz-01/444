"""SQLite journal, several users in one bot. Every signal and the reason behind every decision
is kept per user, so gates and exits can be tuned from recorded data rather than memory."""
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, chat_id TEXT UNIQUE, name TEXT, role TEXT DEFAULT 'user', private_key TEXT,
  live INTEGER DEFAULT 0, paused INTEGER DEFAULT 0, notify_skips INTEGER DEFAULT 0, paper_cash REAL,
  overrides TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created REAL);
CREATE TABLE IF NOT EXISTS traders (
  user_id INTEGER, address TEXT, label TEXT, active INTEGER DEFAULT 1, ticket_usd REAL,
  PRIMARY KEY (user_id, address));
CREATE TABLE IF NOT EXISTS trader_pairs (address TEXT PRIMARY KEY, solana TEXT);
CREATE TABLE IF NOT EXISTS wallets (
  address TEXT PRIMARY KEY, label TEXT, solana TEXT, active INTEGER DEFAULT 1, ticket_usd REAL);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, wallet TEXT, label TEXT, token TEXT, symbol TEXT,
  tx TEXT, block INTEGER, decision TEXT, reason TEXT, details TEXT, user_id INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, token TEXT, symbol TEXT, decimals INTEGER, wallet TEXT, label TEXT,
  opened REAL, closed REAL, status TEXT, paper INTEGER,
  cost_usd REAL, tokens_initial TEXT, tokens_left TEXT, entry_price REAL,
  realized_usd REAL DEFAULT 0, gas_usd REAL DEFAULT 0, last_price REAL, peak_price REAL,
  state TEXT, user_id INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY, position_id INTEGER, ts REAL, side TEXT, tokens TEXT,
  usd REAL, gas_usd REAL, tx TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS ix_pos_status ON positions(status);
CREATE INDEX IF NOT EXISTS ix_sig_ts ON signals(ts);
"""


def norm(a):
    a = str(a or "").strip()
    return a.lower() if a.lower().startswith("0x") else a


class DB:
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.executescript(SCHEMA)
        for table in ("positions", "signals"):  # databases from the single-user version
            cols = {r[1] for r in self.c.execute(f"PRAGMA table_info({table})")}
            if "user_id" not in cols:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER DEFAULT 1")
        for table, col, decl in (("positions", "chain", "TEXT DEFAULT 'rh'"), ("users", "sol_key", "TEXT"),
                                 ("users", "sol_live", "INTEGER DEFAULT 0"), ("users", "muted", "TEXT DEFAULT '[]'"),
                                 ("users", "eth_live", "INTEGER DEFAULT 0"),
                                 ("traders", "source", "TEXT DEFAULT 'manual'"),  # where the trader was imported from
                                 ("positions", "source", "TEXT DEFAULT 'wallet'"),  # entry type: wallet / confluence / tg
                                 ("signals", "source", "TEXT DEFAULT 'wallet'"),
                                 ("users", "live_chains", "TEXT")):  # JSON list of live networks (replaces live/*_live)
            if col not in {r[1] for r in self.c.execute(f"PRAGMA table_info({table})")}:
                self.c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if not self.c.execute("SELECT 1 FROM kv WHERE k='skips_on_v1'").fetchone():
            # copyfomo-style by default: every skipped signal is reported with its reason
            self.c.execute("UPDATE users SET notify_skips=1")
            self.c.execute("INSERT INTO kv(k,v) VALUES('skips_on_v1','true')")
        self.c.commit()

    def migrate(self, cfg, env):
        """Make user #1 (admin) own everything the single-user version stored."""
        if self.c.execute("SELECT 1 FROM users WHERE id=1").fetchone():
            return
        chat = str(env.get("TELEGRAM_CHAT_ID") or self.get("tg_chat") or "") or None
        self.c.execute(
            "INSERT INTO users(id,chat_id,name,role,live,paused,notify_skips,paper_cash,overrides,active,created)"
            " VALUES(1,?,?,?,?,?,?,?,?,1,?)",
            (chat, "admin", "admin", 1 if cfg.get("live") else 0, 1 if self.get("paused") else 0,
             1 if self.get("notify_skips", (cfg.get("telegram") or {}).get("notify_skips")) else 0,
             float(self.get("paper_cash", cfg.get("paper_cash_usd", 500))),
             json.dumps(self.get("overrides") or {}), time.time()))
        for w in self.c.execute("SELECT * FROM wallets").fetchall():
            self.c.execute("INSERT OR IGNORE INTO traders(user_id,address,label,active,ticket_usd) VALUES(1,?,?,?,?)",
                           (w["address"], w["label"], w["active"], w["ticket_usd"]))
            if w["solana"]:
                self.c.execute("INSERT OR IGNORE INTO trader_pairs(address,solana) VALUES(?,?)", (w["address"], w["solana"]))
        self.c.commit()

    # ---------- kv ----------
    def get(self, k, default=None):
        r = self.c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def put(self, k, v):
        self.c.execute("INSERT OR REPLACE INTO kv(k,v) VALUES(?,?)", (k, json.dumps(v)))
        self.c.commit()

    # ---------- users ----------
    def user(self, uid):
        r = self.c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def user_by_chat(self, chat):
        r = self.c.execute("SELECT * FROM users WHERE chat_id=?", (str(chat),)).fetchone()
        return dict(r) if r else None

    def users(self, active_only=True):
        q = "SELECT * FROM users" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self.c.execute(q)]

    def add_user(self, chat, name, paper_cash):
        cur = self.c.execute("INSERT INTO users(chat_id,name,role,paper_cash,overrides,created,notify_skips)"
                             " VALUES(?,?,?,?,?,?,1)", (str(chat), name, "user", paper_cash, "{}", time.time()))
        self.c.commit()
        return cur.lastrowid

    def update_user(self, uid, **fields):
        sets = ",".join(f"{k}=?" for k in fields)
        self.c.execute(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), uid))
        self.c.commit()

    # ---------- traders (per user) and their Solana pairs (shared) ----------
    def traders(self, uid, active_only=False):
        q = "SELECT * FROM traders WHERE user_id=?" + (" AND active=1" if active_only else "") + " ORDER BY rowid"
        return [dict(r) for r in self.c.execute(q, (uid,))]

    def trader(self, uid, key):
        r = self.c.execute("SELECT * FROM traders WHERE user_id=? AND (address=? OR lower(label)=?)"
                           " ORDER BY active DESC", (uid, norm(key), str(key).lower())).fetchone()
        return dict(r) if r else None

    def add_trader(self, uid, address, label, ticket_usd=None, source="manual"):
        self.c.execute("INSERT OR REPLACE INTO traders(user_id,address,label,active,ticket_usd,source) VALUES(?,?,?,1,?,?)",
                       (uid, norm(address), label, ticket_usd, source))
        self.c.commit()

    def set_trader(self, uid, address, **fields):
        for f, v in fields.items():
            self.c.execute(f"UPDATE traders SET {f}=? WHERE user_id=? AND address=?", (v, uid, norm(address)))
        self.c.commit()

    def delete_trader(self, uid, address):
        self.c.execute("DELETE FROM traders WHERE user_id=? AND address=?", (uid, norm(address)))
        self.c.commit()

    def followers(self, address):
        rows = self.c.execute("SELECT t.*, u.id AS uid FROM traders t JOIN users u ON u.id=t.user_id"
                              " WHERE t.address=? AND t.active=1 AND u.active=1 ORDER BY u.id", (norm(address),))
        return [dict(r) for r in rows]

    def watched(self):
        """EVM wallets to watch on Robinhood Chain."""
        rows = self.c.execute("SELECT DISTINCT t.address FROM traders t JOIN users u ON u.id=t.user_id"
                              " WHERE t.active=1 AND u.active=1")
        return [r[0] for r in rows if r[0].startswith("0x")]

    def sol_watch_map(self):
        """Solana wallets to watch -> the trader ids they belong to (a FOMO trader's paired wallet,
        or a Solana-only trader added by address)."""
        out = {}
        rows = self.c.execute("SELECT DISTINCT t.address FROM traders t JOIN users u ON u.id=t.user_id"
                              " WHERE t.active=1 AND u.active=1")
        for (a,) in rows:
            for w in (self.pairs(a) if a.startswith("0x") else {a}):
                out.setdefault(w, set()).add(a)
        return out

    def pairs(self, address):
        r = self.c.execute("SELECT solana FROM trader_pairs WHERE address=?", (norm(address),)).fetchone()
        return {x.strip() for x in ((r["solana"] if r else "") or "").split(",") if x.strip()}

    def set_pair(self, address, solana_csv):
        self.c.execute("INSERT OR REPLACE INTO trader_pairs(address,solana) VALUES(?,?)", (norm(address), solana_csv))
        self.c.commit()

    # ---------- signals ----------
    def signal(self, uid, wallet, label, token, symbol, tx, block, decision, reason, details=None, source="wallet"):
        self.c.execute("INSERT INTO signals(ts,wallet,label,token,symbol,tx,block,decision,reason,details,user_id,source)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (time.time(), wallet, label, token, symbol, tx, block, decision, reason,
                        json.dumps(details or {}, default=str), uid, source))
        self.c.commit()

    def skipped(self, uid, limit=12, exclude=()):
        rows = self.c.execute("SELECT * FROM signals WHERE decision='skipped' AND user_id=? ORDER BY ts DESC LIMIT ?",
                              (uid, limit * 4)).fetchall()
        out = []
        for r in rows:
            if r["reason"] in exclude:
                continue
            x = dict(r)
            x["details"] = json.loads(x["details"] or "{}")
            out.append(x)
            if len(out) >= limit:
                break
        return out

    def skip_reasons(self, uid, limit=8):
        return [(r[0], r[1]) for r in self.c.execute(
            "SELECT reason, count(*) FROM signals WHERE decision='skipped' AND user_id=? GROUP BY reason"
            " ORDER BY 2 DESC LIMIT ?", (uid, limit))]

    # ---------- positions ----------
    def open_position(self, **p):
        cols, qs = ",".join(p.keys()), ",".join("?" * len(p))
        cur = self.c.execute(f"INSERT INTO positions({cols}) VALUES({qs})", tuple(p.values()))
        self.c.commit()
        return cur.lastrowid

    def update_position(self, pid, **fields):
        sets = ",".join(f"{k}=?" for k in fields)
        self.c.execute(f"UPDATE positions SET {sets} WHERE id=?", (*fields.values(), pid))
        self.c.commit()

    def positions(self, uid=None, status="open"):
        if uid is None:
            rows = self.c.execute("SELECT * FROM positions WHERE status=? ORDER BY id", (status,))
        else:
            rows = self.c.execute("SELECT * FROM positions WHERE status=? AND user_id=? ORDER BY id", (status, uid))
        return [self._pos(r) for r in rows.fetchall()]

    def position_by_id(self, pid):
        r = self.c.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
        return self._pos(r) if r else None

    def open_for_token(self, token):
        rows = self.c.execute("SELECT * FROM positions WHERE token=? AND status='open'", (norm(token),)).fetchall()
        return [self._pos(r) for r in rows]

    def holding(self, uid, token):
        return bool(self.c.execute("SELECT 1 FROM positions WHERE user_id=? AND token=? AND status='open'",
                                   (uid, norm(token))).fetchone())

    def last_closed(self, uid, token):
        r = self.c.execute("SELECT closed FROM positions WHERE user_id=? AND token=? AND status!='open'"
                           " ORDER BY closed DESC LIMIT 1", (uid, norm(token))).fetchone()
        return (r["closed"] or 0) if r else None

    def ever_rugged(self, uid, token):
        return bool(self.c.execute("SELECT 1 FROM positions WHERE user_id=? AND token=? AND status='writeoff'",
                                   (uid, norm(token))).fetchone())

    def closed_positions(self, uid, limit=20):
        rows = self.c.execute("SELECT * FROM positions WHERE status!='open' AND user_id=? ORDER BY closed DESC LIMIT ?",
                              (uid, limit)).fetchall()
        return [self._pos(r) for r in rows]

    def stats(self, uid):
        rows = self.c.execute("SELECT label, wallet, cost_usd, realized_usd, gas_usd FROM positions"
                              " WHERE status!='open' AND user_id=?", (uid,)).fetchall()
        return [dict(r) for r in rows]

    def wallet_stats(self, uid, address):
        n = wins = 0
        pnl = 0.0
        for r in self.stats(uid):
            if r["wallet"] != norm(address):
                continue
            x = (r["realized_usd"] or 0) - r["cost_usd"] - (r["gas_usd"] or 0)
            n, wins, pnl = n + 1, wins + (x > 0), pnl + x
        return n, wins, pnl

    def source_stats(self, uid):
        """Closed trades grouped by entry type (positions.source) and by trader import label
        (traders.source), so the user can see which sources actually pay off. Returns two dicts
        {key: [n, wins, pnl]}."""
        rows = self.c.execute(
            "SELECT p.source AS psource, p.cost_usd, p.realized_usd, p.gas_usd, t.source AS tsource"
            " FROM positions p LEFT JOIN traders t ON t.user_id=p.user_id AND t.address=p.wallet"
            " WHERE p.status!='open' AND p.user_id=?", (uid,)).fetchall()
        by_entry, by_import = {}, {}
        for r in rows:
            x = (r["realized_usd"] or 0) - r["cost_usd"] - (r["gas_usd"] or 0)
            for d, key in ((by_entry, r["psource"] or "wallet"), (by_import, r["tsource"] or "manual")):
                acc = d.setdefault(key, [0, 0, 0.0])
                acc[0] += 1
                acc[1] += x > 0
                acc[2] += x
        return by_entry, by_import

    @staticmethod
    def _pos(r):
        p = dict(r)
        p["tokens_initial"] = int(p["tokens_initial"])
        p["tokens_left"] = int(p["tokens_left"])
        p["state"] = json.loads(p["state"] or "{}")
        return p

    def fill(self, position_id, side, tokens, usd, gas_usd, tx, reason):
        self.c.execute("INSERT INTO fills(position_id,ts,side,tokens,usd,gas_usd,tx,reason) VALUES(?,?,?,?,?,?,?,?)",
                       (position_id, time.time(), side, str(tokens), usd, gas_usd, tx, reason))
        self.c.commit()
