-- Application state stays in SQLite; the Google Sheet schema does not change.
CREATE TABLE IF NOT EXISTS event_states(
 event_id TEXT PRIMARY KEY REFERENCES events(id),
 before_json TEXT NOT NULL CHECK(json_valid(before_json))
);
CREATE TABLE IF NOT EXISTS event_groups(
 event_id TEXT PRIMARY KEY REFERENCES events(id), operation_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_group_lookup ON event_groups(operation_id);
CREATE TABLE IF NOT EXISTS telegram_users(
 user_id INTEGER PRIMARY KEY CHECK(user_id>0),
 username TEXT COLLATE NOCASE UNIQUE, observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reply_outbox(
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor INTEGER NOT NULL,
 minimum_role TEXT NOT NULL, method TEXT NOT NULL, data TEXT NOT NULL,
 next_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
);
