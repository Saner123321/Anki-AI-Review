"""Short-lived SQLite connections; local to one profile, no collection SQL writes."""
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS proposals (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL, level TEXT NOT NULL,
              decisions TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL,
              proposal_key TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history (
              id TEXT PRIMARY KEY, proposal_key TEXT NOT NULL, time TEXT NOT NULL,
              before_json TEXT, after_json TEXT, decisions TEXT NOT NULL,
              reason TEXT NOT NULL, state TEXT NOT NULL, undo_name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS manual_edits (
              proposal_key TEXT PRIMARY KEY, fields TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS note_type_edits (
              proposal_key TEXT PRIMARY KEY, format TEXT NOT NULL);
            PRAGMA user_version=1;
            ''')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (uuid.uuid4().hex,))
            self.identity = db.execute("SELECT value FROM metadata WHERE key='identity'").fetchone()[0]

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def import_proposals(self, proposals):
        count = 0
        with self.db() as db:
            for p in proposals:
                cur = db.execute("INSERT OR IGNORE INTO proposals VALUES (?,?,?,'[]','pending',?)",
                                 (p["key"], json.dumps(p, ensure_ascii=False), "normal", now()))
                count += cur.rowcount
                if cur.rowcount:
                    db.execute("INSERT INTO events(time,proposal_key,kind,detail) VALUES(?,?,?,?)", (now(), p["key"], "import", p["reason"]))
        return count

    def proposals(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("""
                SELECT proposals.*, COALESCE(manual_edits.fields, '{}') AS manual_fields,
                       note_type_edits.format AS note_type_edit
                FROM proposals LEFT JOIN manual_edits ON manual_edits.proposal_key = proposals.key
                LEFT JOIN note_type_edits ON note_type_edits.proposal_key = proposals.key
                ORDER BY proposals.created DESC, proposals.rowid DESC
            """)]

    def save_manual_edits(self, key, fields):
        with self.db() as db:
            # Serialize with other queue writers and require a still-pending proposal.
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM proposals WHERE key=?", (key,)).fetchone()
            if row is None or row["status"] != "pending":
                raise ValueError("此提案不再待审，无法保存手动修改。")
            payload = json.dumps(fields, ensure_ascii=False, sort_keys=True)
            db.execute("INSERT OR REPLACE INTO manual_edits VALUES (?,?)", (key, payload))
            db.execute("INSERT INTO events(time,proposal_key,kind,detail) VALUES(?,?,?,?)",
                       (now(), key, "manual_edit", payload))

    def save_note_type(self, key, note_format, decisions, edits, detail):
        with self.db() as db:
            row = db.execute(
                "UPDATE proposals SET decisions=? WHERE key=? AND status='pending'",
                (json.dumps(decisions, ensure_ascii=False), key),
            )
            if row.rowcount != 1:
                raise ValueError("此提案不再待审，无法切换笔记类型。")
            db.execute("INSERT OR REPLACE INTO note_type_edits VALUES (?,?)",
                       (key, json.dumps(note_format, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO manual_edits VALUES (?,?)",
                       (key, json.dumps(edits, ensure_ascii=False)))
            db.execute("INSERT INTO events(time,proposal_key,kind,detail) VALUES(?,?,?,?)",
                       (now(), key, "note_type_change", json.dumps(detail, ensure_ascii=False)))

    def save_decisions(self, key, level, decisions, detail):
        with self.db() as db:
            db.execute("UPDATE proposals SET level=?, decisions=? WHERE key=? AND status='pending'", (level, json.dumps(decisions, ensure_ascii=False), key))
            db.execute("INSERT INTO events(time,proposal_key,kind,detail) VALUES(?,?,?,?)", (now(), key, "decision", json.dumps(detail, ensure_ascii=False)))

    def status(self, key, value):
        with self.db() as db:
            db.execute("UPDATE proposals SET status=? WHERE key=?", (value, key))

    def prepare(self, key, before, after, decisions, reason):
        hid = uuid.uuid4().hex
        name = "AI Review " + hid[:8]
        with self.db() as db:
            db.execute("INSERT INTO history VALUES(?,?,?,?,?,?,?,?,?)", (hid, key, now(), json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), json.dumps(decisions, ensure_ascii=False), reason, "prepared", name))
        return hid, name

    def finish(self, hid, after, state="applied"):
        with self.db() as db:
            db.execute("UPDATE history SET after_json=?,state=? WHERE id=?", (json.dumps(after, ensure_ascii=False), state, hid))
            row = db.execute("SELECT proposal_key FROM history WHERE id=?", (hid,)).fetchone()
            if state == "applied":
                db.execute("UPDATE proposals SET status='applied' WHERE key=?", (row[0],))
            db.execute("INSERT INTO events(time,proposal_key,kind,detail) VALUES(?,?,?,?)", (now(), row[0], state, hid))

    def history(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM history ORDER BY time DESC")]

    def bundle(self):
        with self.db() as db:
            return {table: [dict(r) for r in db.execute(f"SELECT * FROM {table}")]
                    for table in ("proposals", "events", "history", "manual_edits", "note_type_edits")}
