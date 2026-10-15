"""Durable, installation-owned cooperation journal. No filesystem execution broker."""
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3

from src.daemon.state import private_dir


class CooperationError(ValueError):
    def __init__(self, code, message=None, **details):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.details = details


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class AdministrationLock:
    """Real OS lock held throughout service life, also used by offline administration."""
    def __init__(self, directory):
        self.path = Path(directory) / "administration.lock"
        self.stream = None

    def acquire(self):
        if self.path.is_symlink():
            raise CooperationError("store_unavailable")
        stream = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.fchmod(stream.fileno(), 0o600)
        except (OSError, ImportError) as error:
            stream.close()
            raise CooperationError("store_unavailable", "Coordinator or administration already running; OS lock required") from error
        self.stream = stream

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


MAILBOX_SCHEMA = """
CREATE TABLE messages (cursor INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE, repository_id TEXT NOT NULL REFERENCES repositories(id),
    sender_id TEXT NOT NULL REFERENCES participants(id),
    recipient_id TEXT NOT NULL REFERENCES participants(id),
    body TEXT NOT NULL, acknowledged_at REAL, acknowledged_by TEXT REFERENCES participants(id));
CREATE INDEX messages_inbox ON messages(repository_id,recipient_id,acknowledged_at,cursor);
CREATE TABLE message_receipts (repository_id TEXT NOT NULL REFERENCES repositories(id),
    actor_id TEXT NOT NULL REFERENCES participants(id), operation_id TEXT NOT NULL,
    digest TEXT NOT NULL, body TEXT NOT NULL,
    PRIMARY KEY(repository_id,actor_id,operation_id));
"""


SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE repositories (id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
    minimum_cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE participants (id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
    verifier TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE participant_activity (participant_id TEXT PRIMARY KEY REFERENCES participants(id));
CREATE TABLE attempts (repository_id TEXT NOT NULL REFERENCES repositories(id), task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL, participant_id TEXT NOT NULL REFERENCES participants(id),
    contract_revision TEXT, PRIMARY KEY(repository_id, task_id, attempt_id));
CREATE TABLE claims (id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
    participant_id TEXT NOT NULL REFERENCES participants(id), body TEXT NOT NULL);
CREATE TABLE agreements (id TEXT NOT NULL, repository_id TEXT NOT NULL REFERENCES repositories(id),
    body TEXT NOT NULL, PRIMARY KEY(repository_id,id));
CREATE TABLE proposals (repository_id TEXT NOT NULL, agreement_id TEXT NOT NULL, revision INTEGER NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY(repository_id,agreement_id,revision),
    FOREIGN KEY(repository_id,agreement_id) REFERENCES agreements(repository_id,id));
CREATE TABLE acknowledgements (repository_id TEXT NOT NULL, agreement_id TEXT NOT NULL,
    proposal_revision INTEGER NOT NULL, participant_id TEXT NOT NULL REFERENCES participants(id),
    body TEXT NOT NULL, PRIMARY KEY(repository_id,agreement_id,proposal_revision,participant_id),
    FOREIGN KEY(repository_id,agreement_id,proposal_revision) REFERENCES proposals(repository_id,agreement_id,revision));
CREATE TABLE dependencies (id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id),
    agreement_id TEXT, body TEXT NOT NULL,
    FOREIGN KEY(repository_id,agreement_id) REFERENCES agreements(repository_id,id));
CREATE TABLE artifacts (id TEXT NOT NULL, repository_id TEXT NOT NULL REFERENCES repositories(id),
    participant_id TEXT NOT NULL REFERENCES participants(id), body TEXT NOT NULL, PRIMARY KEY(repository_id,id));
CREATE TABLE acceptances (repository_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
    consumer_id TEXT NOT NULL REFERENCES participants(id), body TEXT NOT NULL,
    PRIMARY KEY(repository_id,artifact_id,consumer_id),
    FOREIGN KEY(repository_id,artifact_id) REFERENCES artifacts(repository_id,id));
CREATE TABLE checkpoints (repository_id TEXT NOT NULL REFERENCES repositories(id),
    participant_id TEXT NOT NULL REFERENCES participants(id), checkpoint_id TEXT NOT NULL,
    body TEXT NOT NULL, PRIMARY KEY(repository_id,participant_id,checkpoint_id));
CREATE TABLE events (cursor INTEGER PRIMARY KEY AUTOINCREMENT, repository_id TEXT NOT NULL REFERENCES repositories(id),
    agreement_id TEXT, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE receipts (repository_id TEXT NOT NULL REFERENCES repositories(id),
    participant_id TEXT NOT NULL REFERENCES participants(id), operation_id TEXT NOT NULL,
    digest TEXT NOT NULL, body TEXT NOT NULL, agreement_id TEXT, created_at REAL NOT NULL,
    PRIMARY KEY(repository_id,participant_id,operation_id));
CREATE INDEX events_repository ON events(repository_id,cursor);
CREATE INDEX claims_repository ON claims(repository_id);
CREATE INDEX participants_repository ON participants(repository_id);
""" + MAILBOX_SCHEMA


class Store:
    def __init__(self, directory, *, timeout=1.0):
        self.directory = Path(directory)
        self.path = self.directory / "cooperation.sqlite3"
        self.timeout = timeout

    def connect(self):
        if self.path.is_symlink():
            raise CooperationError("store_unavailable", "Journal must not be a symlink")
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def initialize(self):
        private_dir(self.directory)
        with closing(self.connect()) as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise CooperationError("store_unavailable", "Unsupported journal schema; preserve data and upgrade explicitly")
            # DELETE/FULL is deliberate: local filesystems, short transactions, durable receipts.
            db.execute("PRAGMA journal_mode=DELETE")
            if version == 0:
                try:
                    db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=3;\nCOMMIT;")
                except BaseException:
                    if db.in_transaction:
                        db.rollback()
                    raise
            elif version in (1, 2):
                # Earlier journals cannot prove that a registration was unused.
                # Conservatively protect every existing participant from retirement.
                try:
                    activity = """
                        CREATE TABLE participant_activity (participant_id TEXT PRIMARY KEY REFERENCES participants(id));
                        INSERT INTO participant_activity SELECT id FROM participants;
                    """ if version == 1 else ""
                    db.executescript("BEGIN IMMEDIATE;\n" + activity + MAILBOX_SCHEMA
                                     + "\nPRAGMA user_version=3;\nCOMMIT;")
                except BaseException:
                    if db.in_transaction:
                        db.rollback()
                    raise
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def transaction(self, *, write=True):
        db = self.connect()
        try:
            # Bounded admission retries tolerate short concurrent commits. Never
            # replay a transaction body or an ambiguous COMMIT automatically.
            for attempt in range(3):
                try:
                    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                    break
                except sqlite3.OperationalError as error:
                    if attempt == 2 or not ("locked" in str(error).lower() or "busy" in str(error).lower()):
                        raise
            yield db
            db.commit()
        except sqlite3.OperationalError as error:
            db.rollback()
            code = "busy" if "locked" in str(error).lower() or "busy" in str(error).lower() else "store_unavailable"
            raise CooperationError(code, "Journal transaction unavailable") from error
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


def bodies(db, table, repository_id):
    # Table names are internal constants, never request data.
    return [json.loads(row[0]) for row in db.execute(f"SELECT body FROM {table} WHERE repository_id=?", (repository_id,))]
