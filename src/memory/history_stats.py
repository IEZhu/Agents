"""Answer counts from the workspaces' history files, for the daemon's statistics (#187).

Text parsing only: no embeddings and no index. A file is read line by line into a compact
summary of its entries (time, agent, persona action, app) that never holds the text of a query
or an answer. The summary is reused while the file's inode, size and modification time stay the
same, so a poll re-reads only what changed: ``history.md`` after an append, never an unchanged
month archive. ``summarize`` blocks on file I/O; the daemon runs it off the event loop.
"""
from __future__ import annotations

import datetime as dt
import heapq
import json
import os
import re
import stat
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from src.memory.history import _FIELD_RE, _HEADER_RE, _archive_names, _reading

DAYS = 30  # the window of the counts and of the chart
RECENT = 50  # the newest answers across workspaces
ACTIONS = ("keep", "switch", "refresh", "restore")
UNKNOWN = "unknown"
_PERSONA = re.compile(r"(?:^|\s)Persona \((?:client-reported|unverified|mismatch)\): (?P<rest>.*)$")
_DEFAULT_ACTION = re.compile(r"Agent: (?P<agent>[A-Za-z0-9_.-]{1,64})(?:\s|$)")
_AGENT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_APP = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


@dataclass(frozen=True)
class Answer:
    """One history entry as the statistics see it."""
    id: str
    time: str  # the heading's timestamp, ISO 8601 in UTC
    agent: str  # the persona line's agent, else the default action's, else "unknown"
    action: Optional[str]  # keep, switch, refresh, restore or "other"; None without a persona line
    client: Optional[str]  # the app that logged it (``client`` in ``**Meta:**``), when known


def _persona(action_text: str) -> tuple[Optional[str], Optional[str], bool]:
    """``(agent, action, found)`` from the attribution that ``log_interaction`` appends to the action."""
    match = None
    for match in _PERSONA.finditer(action_text):
        pass
    if match is None:
        return None, None, False
    parts = [part.strip() for part in match.group("rest").split(";")]
    agent = parts[0] if parts and "=" not in parts[0] and _AGENT.fullmatch(parts[0]) else None
    action = next((part[len("action="):] for part in parts if part.startswith("action=")), None)
    return agent, (action if action in ACTIONS else "other"), True


def _client(meta_text: str) -> Optional[str]:
    if '"client"' not in meta_text:
        return None
    try:
        meta = json.loads(meta_text)
    except ValueError:
        return None
    client = meta.get("client") if isinstance(meta, dict) else None
    return client if isinstance(client, str) and _APP.fullmatch(client) else None


def attribution(action_text: str) -> tuple[str, Optional[str]]:
    """``(agent, persona action)`` of an entry's action: the persona line's agent, else the default
    action's, else "unknown"; the action is None without a persona line."""
    agent, action, found = _persona(action_text)
    if agent is None:
        default = _DEFAULT_ACTION.match(action_text)
        agent = default.group("agent") if default else UNKNOWN
    return agent, action if found else None


def _answer(entry_id: str, timestamp: str, action_text: str, meta_text: str) -> Answer:
    return Answer(entry_id, timestamp, *attribution(action_text), _client(meta_text))


def _answers(lines: Iterable[str]) -> list[Answer]:
    """The entries of a history file's lines, in file order; only their heading, action and meta."""
    out: list[Answer] = []
    current: Optional[list[str]] = None
    for line in lines:
        header = _HEADER_RE.match(line)
        if header:
            if current is not None:
                out.append(_answer(*current))
            current = [header.group("id"), header.group("ts"), "", ""]
            continue
        if current is None:
            continue
        field = _FIELD_RE.match(line.rstrip("\r\n"))
        if field is None:
            continue
        name = field.group("name").lower()
        if name == "action":
            current[2] = field.group("value")
        elif name == "meta":
            current[3] = field.group("value")
    if current is not None:
        out.append(_answer(*current))
    return out


class FileSummaries:
    """Answers of history files by path, kept while a file's inode, size and mtime stay the same."""

    def __init__(self, max_files: int = 512):
        self._items: "OrderedDict[str, tuple]" = OrderedDict()
        self._max_files = max_files
        self._lock = threading.Lock()
        self.reads = 0  # files parsed, not served from the cache

    def answers(self, path: str) -> tuple[Answer, ...]:
        """The answers of ``path``; empty when it does not exist. Other read errors propagate."""
        try:
            info = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            return ()
        if not stat.S_ISREG(info.st_mode):
            return ()
        key = (info.st_ino, info.st_size, info.st_mtime_ns)  # a replaced file differs even at equal size
        with self._lock:
            cached = self._items.get(path)
            if cached is not None and cached[0] == key:
                self._items.move_to_end(path)
                return cached[1]
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as stream:
            answers = tuple(_answers(stream))
            after = os.fstat(stream.fileno())
        with self._lock:
            self.reads += 1
            if (after.st_ino, after.st_size, after.st_mtime_ns) == key:  # unchanged while read
                self._items[path] = (key, answers)
                self._items.move_to_end(path)
                while len(self._items) > self._max_files:
                    self._items.popitem(last=False)
        return answers


def _workspace_answers(root: Path, files: FileSummaries, since_month: str) -> Iterable[Answer]:
    """This workspace's answers: its archives from ``since_month`` on, a pending rotation and
    ``history.md``. An entry that a rotation moves meanwhile shows in two files and counts once; the
    same content logged again later (the writer deduplicates only its recent entries) has the same
    id at another time and counts again."""
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a directory")
    history = str(root / "history.md")
    archive_dir = str(root / "history")
    chosen: dict[tuple[str, str], Answer] = {}
    for name in _archive_names(archive_dir):
        if name[:7] >= since_month:
            for answer in files.answers(os.path.join(archive_dir, name)):
                chosen[(answer.id, answer.time)] = answer
    pending = history + ".rotating"
    if os.path.exists(history) or os.path.exists(pending):
        with _reading(history):
            for path in (pending, history):
                for answer in files.answers(path):
                    chosen[(answer.id, answer.time)] = answer
    return chosen.values()


def _local(timestamp: str, zone) -> Optional[dt.datetime]:
    try:
        moment = dt.datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(zone)


def _ranked(counter: Counter, key: str) -> list[dict]:
    return [{key: name, "answers": count}
            for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def summarize(workspaces: Iterable[tuple[str, str, Path]], *, now: dt.datetime,
              files: FileSummaries) -> dict:
    """Answer counts over the last ``DAYS`` local days, and the ``RECENT`` newest answers.

    ``workspaces`` holds ``(workspace id, repository name, root)``. ``now`` is an aware time in the
    zone the days are counted in. A workspace whose directory is gone or unreadable is listed in
    ``skipped`` and the others still count.
    """
    zone = now.tzinfo
    today = now.date()
    first = today - dt.timedelta(days=DAYS - 1)
    # Archives are named by the UTC month of their entries; a day earlier covers any zone.
    since_month = (first - dt.timedelta(days=1)).strftime("%Y-%m")
    per_day, per_agent, per_action, per_app, per_repository = Counter(), Counter(), Counter(), Counter(), Counter()
    seen, skipped = [], []
    for workspace, name, root in workspaces:
        try:
            answers = list(_workspace_answers(Path(root), files, since_month))
        except OSError as error:
            reason = "missing" if isinstance(error, (FileNotFoundError, NotADirectoryError)) else "unreadable"
            skipped.append({"workspace": workspace, "name": name, "reason": reason})
            continue
        for answer in answers:
            moment = _local(answer.time, zone)
            if moment is None:
                continue
            seen.append((moment, answer.id, workspace, name, answer))
            if not first <= moment.date() <= today:
                continue
            per_day[moment.date()] += 1
            per_agent[answer.agent] += 1
            per_action[answer.action or "none"] += 1
            per_app[answer.client or UNKNOWN] += 1
            per_repository[(workspace, name)] += 1
    days = [first + dt.timedelta(days=offset) for offset in range(DAYS)]
    newest = heapq.nlargest(RECENT, seen, key=lambda item: (item[0], item[1]))
    return {
        "today": per_day[today],
        "last_7_days": sum(per_day[day] for day in days[-7:]),
        "last_30_days": sum(per_day[day] for day in days),
        "per_day": [{"date": day.isoformat(), "answers": per_day[day]} for day in days],
        "per_agent": _ranked(per_agent, "agent"),
        "per_repository": [{"workspace": workspace, "name": name, "answers": count}
                           for (workspace, name), count in sorted(per_repository.items(),
                                                                  key=lambda item: (-item[1], item[0][1]))],
        "per_action": {action: per_action[action] for action in (*ACTIONS, "other", "none")},
        "per_app": _ranked(per_app, "app"),
        "events": [{"id": answer.id, "time": answer.time, "agent": answer.agent, "action": answer.action,
                    "workspace": workspace, "repository": name, "app": answer.client}
                   for _moment, _id, workspace, name, answer in newest],
        "skipped": skipped,
    }
