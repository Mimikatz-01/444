-- Schema of the previous rhcopy version (as in data-dev/bot.db): no users.muted / users.eth_live yet.
CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE wallets (
  address TEXT PRIMARY KEY, label TEXT, solana TEXT, active INTEGER DEFAULT 1, ticket_usd REAL);
CREATE TABLE signals (
  id INTEGER PRIMARY KEY, ts REAL, wallet TEXT, label TEXT, token TEXT, symbol TEXT,
  tx TEXT, block INTEGER, decision TEXT, reason TEXT, details TEXT, user_id INTEGER DEFAULT 1);
CREATE TABLE positions (
  id INTEGER PRIMARY KEY, token TEXT, symbol TEXT, decimals INTEGER, wallet TEXT, label TEXT,
  opened REAL, closed REAL, status TEXT, paper INTEGER,
  cost_usd REAL, tokens_initial TEXT, tokens_left TEXT, entry_price REAL,
  realized_usd REAL DEFAULT 0, gas_usd REAL DEFAULT 0, last_price REAL, peak_price REAL,
  state TEXT, user_id INTEGER DEFAULT 1, chain TEXT DEFAULT 'rh');
CREATE TABLE fills (
  id INTEGER PRIMARY KEY, position_id INTEGER, ts REAL, side TEXT, tokens TEXT,
  usd REAL, gas_usd REAL, tx TEXT, reason TEXT);
CREATE INDEX ix_pos_status ON positions(status);
CREATE INDEX ix_sig_ts ON signals(ts);
CREATE TABLE users (
  id INTEGER PRIMARY KEY, chat_id TEXT UNIQUE, name TEXT, role TEXT DEFAULT 'user', private_key TEXT,
  live INTEGER DEFAULT 0, paused INTEGER DEFAULT 0, notify_skips INTEGER DEFAULT 0, paper_cash REAL,
  overrides TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created REAL, sol_key TEXT, sol_live INTEGER DEFAULT 0);
CREATE TABLE traders (
  user_id INTEGER, address TEXT, label TEXT, active INTEGER DEFAULT 1, ticket_usd REAL,
  PRIMARY KEY (user_id, address));
CREATE TABLE trader_pairs (address TEXT PRIMARY KEY, solana TEXT);

INSERT INTO kv VALUES ('paper_cash', '500.0'), ('tg_chat', '"1000001"'), ('last_block', '79030010');
INSERT INTO wallets VALUES ('0xddd462bb053b57d5d73c9615e11a7284cfee9233', 'iruletrenches', 'DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6', 1, NULL);
INSERT INTO users (id, chat_id, name, role, live, paused, notify_skips, paper_cash, overrides, active, created, sol_live)
  VALUES (1, '1000001', 'admin', 'admin', 0, 0, 1, 500.0, '{}', 1, 1790949568.6, 0),
         (2, '1000002', 'friend', 'user', 0, 0, 0, 469.6, '{"sizing.ticket_usd": 20}', 1, 1790949680.8, 0);
INSERT INTO traders VALUES (1, '0xddd462bb053b57d5d73c9615e11a7284cfee9233', 'iruletrenches', 1, NULL),
  (2, '0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', 'Katuchi_xyz', 1, 50.0),
  (2, '0xae57f8a2f120fce90f1afb5bb0ad934db3bc6855', 'solgavvv', 1, 50.0);
INSERT INTO trader_pairs VALUES ('0xddd462bb053b57d5d73c9615e11a7284cfee9233', 'DyXg3Xp6BoMq5K6Nmdb7hEzPMpkRWH2aXHHZhUgN1gd6'),
  ('0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', '4XPvoZQrTnGZuTiYqMm2z6JbeHMtsZQfJyYMJCwgMKot');
INSERT INTO positions VALUES
  (1, '5GDX5fJTQns4arM1J94wjxdA8KXFsV5qW46MLFpHpump', 'OMNI', 6, '0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', 'Katuchi_xyz',
   1790975249.8, 1790976482.6, 'closed', 1, 20.0, '176872195841', '0', 0.000113, 9.17, 0.047, 0.0000537, 0.000135,
   '{"origin_tx": "21x5qz6S"}', 2, 'sol'),
  (2, 'A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS', 'ZEC', 8, '0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', 'Katuchi_xyz',
   1791022947.8, NULL, 'open', 1, 19.57, '1484122', '1484122', 1318.6, 0.0, 0.024, 1317.9, 1321.0,
   '{"origin_tx": "5gc2UDEy"}', 2, 'sol');
INSERT INTO fills VALUES (1, 1, 1790975249.8, 'buy', '176872195841', 20.0, 0.024, 'paper', 'entry'),
  (2, 1, 1790976482.6, 'sell', '176872195841', 9.17, 0.024, 'paper', 'stop_loss'),
  (3, 2, 1791022947.8, 'buy', '1484122', 19.57, 0.024, 'paper', 'entry');
INSERT INTO signals VALUES
  (1, 1791024435.2, '0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', 'Katuchi_xyz', 'fCUBpdeRn76xfRaa4UPHDauGeRG3EvMB3MjuRgdpump',
   'DARKPOOL', 'sig1', 0, 'skipped', 'impact', '{"chain": "sol", "impact": -7.31, "price": 0.00007}', 2),
  (2, 1791022947.8, '0x3571157f5f7c4bda6ddd9d91d2a2ef6712ab6c0e', 'Katuchi_xyz', 'A7bdiYdS5GjqGFtxf17ppRHtDKPkkRqbKtR27dxvQXaS',
   'ZEC', 'sig2', 0, 'bought', '', '{"chain": "sol"}', 2),
  (3, 1791014945.2, '0xddd462bb053b57d5d73c9615e11a7284cfee9233', 'iruletrenches', '0x02fa7c6b92a501aa6389814479d355413f9784f8',
   'SICAT', '0x1', 1, 'skipped', 'not_a_swap', '{}', 1);
