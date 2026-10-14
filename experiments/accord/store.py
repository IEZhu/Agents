"""Small durable ledger; all records are private to the trusted host."""

from contextlib import contextmanager
import json
import sqlite3

from .ir import canonical, require


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id))")
            db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, value TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def get(db, kind, key, optional=False):
        row = db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            require(optional, f"unknown {kind}: {key}")
            return None
        return json.loads(row[0])

    @staticmethod
    def put(db, kind, key, value):
        encoded = canonical(value).decode("utf-8")
        db.execute("INSERT INTO records VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value",
                   (kind, key, encoded))
        db.execute("INSERT INTO events(kind,value) VALUES (?,?)", (kind, encoded))

    def inspect(self, kind, key):
        with self.transaction() as db:
            return self.get(db, kind, key)

    def events(self):
        with self.transaction() as db:
            return [{"seq": seq, "kind": kind, "value": json.loads(value)}
                    for seq, kind, value in db.execute("SELECT seq,kind,value FROM events ORDER BY seq")]
