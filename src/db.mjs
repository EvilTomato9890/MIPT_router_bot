import { DatabaseSync, backup } from 'node:sqlite';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { uuid, hash, assert, redact } from './util.mjs';

export class Store {
  constructor(path = ':memory:') {
    if (path !== ':memory:') mkdirSync(dirname(resolve(path)), { recursive: true });
    this.db = new DatabaseSync(path, { timeout: 5000, enableForeignKeyConstraints: true });
    this.db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;');
    this.db.exec(readFileSync(new URL('../migrations/001_initial.sql', import.meta.url), 'utf8'));
    this.run('INSERT OR IGNORE INTO schema_migrations VALUES(1,?)', new Date().toISOString());
  }
  get(sql, ...args) {
    return this.db.prepare(sql).get(...args);
  }
  all(sql, ...args) {
    return this.db.prepare(sql).all(...args);
  }
  run(sql, ...args) {
    return this.db.prepare(sql).run(...args);
  }
  tx(fn) {
    this.db.exec('BEGIN IMMEDIATE');
    try {
      const result = fn();
      assert(!(result instanceof Promise), 'Транзакция должна быть синхронной');
      this.db.exec('COMMIT');
      return result;
    } catch (e) {
      this.db.exec('ROLLBACK');
      throw e;
    }
  }
  atomic(actor, key, payload, fn) {
    assert(key && key.length <= 200, 'Нужен ключ операции');
    return this.tx(() => {
      const old = this.get('SELECT * FROM requests WHERE key=?', key),
        signature = hash(payload);
      if (old) {
        assert(
          old.actor === actor && old.signature === signature,
          'Ключ уже использован для другой операции',
        );
        return JSON.parse(old.result);
      }
      const result = fn(uuid());
      this.run(
        'INSERT INTO requests VALUES(?,?,?,?)',
        key,
        actor,
        signature,
        JSON.stringify(result ?? null),
      );
      return result;
    });
  }
  setting(key, fallback = null) {
    const r = this.get('SELECT value FROM settings WHERE key=?', key);
    return r ? JSON.parse(r.value) : fallback;
  }
  set(key, value) {
    this.run(
      'INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
      key,
      JSON.stringify(value),
    );
  }
  event(
    actor,
    object,
    action,
    before,
    after,
    reason = null,
    correctionOf = null,
    at = new Date().toISOString(),
  ) {
    const id = uuid();
    this.run(
      'INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',
      id,
      at,
      actor,
      object,
      action,
      JSON.stringify(redact(before ?? null)),
      JSON.stringify(redact(after ?? null)),
      reason,
      correctionOf,
    );
    return id;
  }
  draft(user, kind, data, now = new Date()) {
    const id = uuid();
    this.run(
      'INSERT INTO drafts VALUES(?,?,?,?,?)',
      id,
      user,
      kind,
      JSON.stringify(data),
      new Date(+now + 1800000).toISOString(),
    );
    return id;
  }
  getDraft(user, id, now = new Date()) {
    const r = this.get('SELECT * FROM drafts WHERE id=?', id);
    assert(
      r && r.user_id === user && r.expires_at > now.toISOString(),
      'Подтверждение устарело или принадлежит другому сотруднику',
    );
    return { ...r, data: JSON.parse(r.data) };
  }
  async backup(path) {
    mkdirSync(dirname(resolve(path)), { recursive: true });
    await backup(this.db, path);
  }
  close() {
    this.db.close();
  }
}
