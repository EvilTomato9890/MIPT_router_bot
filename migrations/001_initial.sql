PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roles(user_id INTEGER PRIMARY KEY CHECK(user_id>0), role TEXT NOT NULL CHECK(role IN ('admin','operator','observer')));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS routers(
 id TEXT PRIMARY KEY, inventory TEXT NOT NULL COLLATE NOCASE UNIQUE,
 serial_norm TEXT, mac_norm TEXT,
 status TEXT NOT NULL CHECK(status IN ('stored','issued','lost','unknown','written_off')),
 condition TEXT NOT NULL CHECK(condition IN ('working','needs_check','broken','repair')),
 version INTEGER NOT NULL, data TEXT NOT NULL CHECK(json_valid(data))
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_serial ON routers(serial_norm) WHERE serial_norm IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS unique_mac ON routers(mac_norm) WHERE mac_norm IS NOT NULL;
CREATE TABLE IF NOT EXISTS loans(
 id TEXT PRIMARY KEY, router_id TEXT NOT NULL REFERENCES routers(id) DEFERRABLE INITIALLY DEFERRED,
 open INTEGER NOT NULL CHECK(open IN (0,1)), data TEXT NOT NULL CHECK(json_valid(data))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_loan ON loans(router_id) WHERE open=1;
CREATE TABLE IF NOT EXISTS events(
 id TEXT PRIMARY KEY, at TEXT NOT NULL, actor INTEGER NOT NULL, object_id TEXT NOT NULL,
 action TEXT NOT NULL, before_json TEXT, after_json TEXT, reason TEXT, correction_of TEXT REFERENCES events(id)
);
CREATE TRIGGER IF NOT EXISTS event_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'Журнал нельзя изменять'); END;
CREATE TRIGGER IF NOT EXISTS event_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'Журнал нельзя удалять'); END;
CREATE TABLE IF NOT EXISTS requests(key TEXT PRIMARY KEY, actor INTEGER NOT NULL, signature TEXT NOT NULL, result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(
 router_id TEXT PRIMARY KEY REFERENCES routers(id), group_id TEXT NOT NULL, version INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 next_at TEXT NOT NULL, first_at TEXT NOT NULL, last_error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(router_id TEXT PRIMARY KEY REFERENCES routers(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts(router_id TEXT PRIMARY KEY REFERENCES routers(id), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(user_id INTEGER PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_updates(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_members(campaign_id TEXT NOT NULL REFERENCES campaigns(id), loan_id TEXT NOT NULL REFERENCES loans(id), data TEXT NOT NULL, PRIMARY KEY(campaign_id,loan_id));
CREATE TABLE IF NOT EXISTS reminders(
 key TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id), target INTEGER NOT NULL,
 due_at TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
