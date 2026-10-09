"""Task Scheduler tasks of the current user, for the scheduled sync run (`src.user_sync.schedule`)
and the shared service (`src.daemon.service`). Standard library only.

A task here runs only while its user is logged on (``InteractiveToken``), with least privilege,
so no password is stored and no administrator rights are needed. Its triggers are a logon
trigger and a time trigger repeated every few minutes from the time of registration; without
a duration the repetition never ends, and it goes on after logoffs and reboots, as with
``schtasks /SC MINUTE``. ``IgnoreNew`` keeps one instance: a repetition while the task runs
starts nothing.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
from xml.etree import ElementTree

NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


def windowless(python: str) -> str:
    """``pythonw.exe`` next to the interpreter when it exists, so no console window opens on each run."""
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.is_file() else python


def windows_user() -> str | None:
    user, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    return (f"{domain}\\{user}" if domain else user) if user else None


def schtasks_text(data: bytes) -> str:
    """``schtasks`` output: UTF-16 after a byte order mark, otherwise ASCII-compatible."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    return data.replace(b"\x00", b"").decode("utf-8", "replace")


@dataclass(frozen=True)
class Task:
    """What a task runs and when. ``command`` is the program, ``arguments`` its argv after it."""
    description: str
    command: str
    arguments: tuple[str, ...]
    working_directory: str
    interval_minutes: int
    time_limit: str  # an ISO 8601 duration; PT0S for none
    priority: int  # 4-6 for interactive work, 7 (Task Scheduler's default) for background tasks
    at_logon: bool = True  # the logon trigger is enabled
    repeating: bool = True  # the repeating time trigger is enabled
    user: str | None = None


def definition(task: Task) -> bytes:
    """The task as Task Scheduler XML, in UTF-16 as ``schtasks /XML`` reads it."""
    root = ElementTree.Element("Task", {"version": "1.2", "xmlns": NAMESPACE})

    def add(parent: ElementTree.Element, tag: str, text: str | None = None, **attributes: str):
        node = ElementTree.SubElement(parent, tag, attributes)
        node.text = text
        return node

    add(add(root, "RegistrationInfo"), "Description", task.description)
    triggers = add(root, "Triggers")
    logon = add(triggers, "LogonTrigger")
    add(logon, "Enabled", "true" if task.at_logon else "false")
    if task.user:
        add(logon, "UserId", task.user)  # this user's logon: no administrator rights needed
    start = add(triggers, "TimeTrigger")
    repetition = add(start, "Repetition")
    minutes = task.interval_minutes
    add(repetition, "Interval", "PT1H" if minutes == 60 else f"PT{minutes}M")
    add(repetition, "StopAtDurationEnd", "false")
    add(start, "StartBoundary", datetime.now().replace(microsecond=0).isoformat())
    add(start, "Enabled", "true" if task.repeating else "false")
    principal = add(add(root, "Principals"), "Principal", id="Author")
    if task.user:
        add(principal, "UserId", task.user)
    add(principal, "LogonType", "InteractiveToken")  # only while logged on; no stored password
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(root, "Settings")
    for tag, value in (("MultipleInstancesPolicy", "IgnoreNew"), ("DisallowStartIfOnBatteries", "false"),
                       ("StopIfGoingOnBatteries", "false"), ("AllowHardTerminate", "true"),
                       ("StartWhenAvailable", "false"), ("RunOnlyIfNetworkAvailable", "false")):
        add(settings, tag, value)
    idle = add(settings, "IdleSettings")
    add(idle, "StopOnIdleEnd", "false")
    add(idle, "RestartOnIdle", "false")
    for tag, value in (("AllowStartOnDemand", "true"), ("Enabled", "true"), ("Hidden", "true"),
                       ("RunOnlyIfIdle", "false"), ("WakeToRun", "false"),
                       ("ExecutionTimeLimit", task.time_limit), ("Priority", str(task.priority))):
        add(settings, tag, value)
    execute = add(add(root, "Actions", Context="Author"), "Exec")
    add(execute, "Command", f'"{task.command}"')  # quoted for spaces; a Windows path cannot hold '"'
    # The interpreter splits its command line by the C runtime's rules, which list2cmdline quotes for.
    add(execute, "Arguments", subprocess.list2cmdline(list(task.arguments)))
    add(execute, "WorkingDirectory", task.working_directory)  # never quoted: "Start in" rejects quotes
    ElementTree.indent(root)
    text = '<?xml version="1.0" encoding="UTF-16"?>\n' + ElementTree.tostring(root, encoding="unicode") + "\n"
    return text.encode("utf-16")


def enabled_triggers(document: str) -> dict[str, bool]:
    """``{"LogonTrigger": …, "TimeTrigger": …}`` of a registered task's XML (``schtasks /Query /XML``).

    The XML is not localized, unlike the status text of ``schtasks /Query``. A trigger without
    an ``Enabled`` element is enabled.
    """
    # schtasks declares UTF-16 even where its output reaches us decoded: parse the text without it.
    root = ElementTree.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", document.lstrip("﻿")))
    found = {}
    for kind in ("LogonTrigger", "TimeTrigger"):
        node = root.find(f"{{{NAMESPACE}}}Triggers/{{{NAMESPACE}}}{kind}")
        if node is not None:
            flag = node.find(f"{{{NAMESPACE}}}Enabled")
            found[kind] = flag is None or (flag.text or "").strip().lower() == "true"
    return found
