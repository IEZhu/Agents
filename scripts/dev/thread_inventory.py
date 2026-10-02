"""Inventory of a Claude Code thread from its transcript, for flows/thread-close.md.

    python scripts/dev/thread_inventory.py --session <uuid>
    python scripts/dev/thread_inventory.py --transcript <part.jsonl> [--transcript <part.jsonl> ...]
    python scripts/dev/thread_inventory.py --project-dir <working directory> --latest

Prints one JSON object and only reads. The inventory is evidence of what the thread
did, taken from its transcript instead of the model's memory, which may have been
compacted. It is not evidence that the work is correct or still current: the flow
verifies every item against the live state.

`--session` looks for `<uuid>.jsonl` under `projects/*/` of `$CLAUDE_CONFIG_DIR` and
every `~/.claude*` directory. In Claude Code the session id is the directory name
above the session's scratchpad. `--latest` takes the newest transcript of a project and
lists the other transcripts written in the last hour, because several sessions can
run in one project at the same time.

Work delegated to subagents and workflow agents is read from the transcripts in the
session's directory (`<uuid>/subagents/`, `<uuid>/workflows/`); its items carry the
agent's file name in `by`. Background tasks come only from the results of the tools
that start or stop them, including commands that a timeout moved to the background.

Shell commands are split into simple commands at unquoted operators and line ends,
so quoted text is never read as a command, while command substitutions and the
scripts of `bash -c` are. A heredoc body counts as commands only when a shell reads
it; otherwise it is data, such as a commit message or a file, and its references
are not collected. Only the command substitutions of an unquoted body (`<<EOF`)
run. A heredoc without its delimiter line takes the rest of the command, as the
shell reads it.

Token usage is counted once per model response: the transcript repeats a response's
usage on every entry (thinking, text, tool call) that the response produced. Texts
are shortened, common credential shapes are masked in every printed string, paths
included, and tool inputs are reduced to the tool name and its target identifiers.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

MAX_TEXT = 500
MAX_COMMAND = 200
MAX_DEPTH = 20  # nested substitutions and subshells parsed recursively
GITHUB_URL = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")
SHORT_REF = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d+)\b")
SHELL_SPECIAL = re.compile(r"[\\'\"#<\n;|&()`]")
QUOTE_END = {"'": re.compile(r"'"), "$'": re.compile(r"['\\]"), '"': re.compile(r'["\\`]|\$\(')}
HEREDOC = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|(\\?)([^\s;&|<>()'\"]+))")
BODY_EXPANSION = re.compile(r"\\|`|\$\(")  # what an unquoted heredoc body runs, and its escape
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
COMMAND_PREFIXES = {"if", "then", "elif", "else", "while", "until", "do", "!", "{", "time", "exec", "command",
                    "builtin", "nohup", "env", "sudo"}
KEYWORD_PREFIX = re.compile(r"^(?:(?:if|then|elif|else|while|until|do|!|\{)\s+)+")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")  # NAME=value before a program, a quoted value with spaces too
TOKEN_SPLIT = re.compile(r"[\s'\"`|;&()<>=,]+")
NOTICE = re.compile(r"<task-notification>(.*?)(?:</task-notification>|$)", re.S)
TASK_ID = re.compile(r"<task-id>(\w+)</task-id>")
STATUS = re.compile(r"<status>(\w+)</status>")
# Background work is read only from the results of the tools that start or stop it: the same
# words in a file, a log or another command's output start or stop nothing.
STARTED = {
    "Bash": re.compile(r"\s*Command (?:running in background with ID: |did not complete within .*?"
                       r"moved to the background \(ID: )(\w+)"),
    "Monitor": re.compile(r"\s*Monitor started \(task (\w+)"),
    "Workflow": re.compile(r"\s*Workflow launched in background\. Task ID: (\w+)"),
    "Agent": re.compile(r"\s*Async agent launched.*?agentId: (\w+)", re.S),
}
STOPPED = re.compile(r"Successfully stopped task: (\w+)")  # in TaskStop results
MEMORY_PATH = re.compile(r"/projects/[^/]+/memory/")
# Phrases with which a model takes back its own earlier statement. They are candidates
# for the audit, not conclusions: the flow reads each one in context.
CORRECTION = re.compile(
    r"(?i)\b(?:my mistake|i was wrong|my error|i misstated|i retract|that was wrong|incorrectly (?:stated|claimed|said))\b"
    r"|мо(?:я|ей|ю) ошибк|я ошиб(?:ся|лась)|по моей вине|(?:неверн|ошибочн)\w* (?:подсч|утвержд|сказал|написал|назвал)"
    r"|был[аои]? неверн")
# Common credential shapes, masked in every text the inventory prints.
SECRET = re.compile(
    r"(?i)(authorization:\s*(?:bearer\s+|basic\s+|token\s+)?|bearer\s+"
    r"|(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+"
    r"|\bsk-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
WRITE_VERBS = {
    "add", "append", "approve", "archive", "assign", "attach", "batch", "cancel", "close", "comment", "complete",
    "create", "delete", "disable", "edit", "enable", "execute", "import", "insert", "invite", "link", "mark",
    "merge", "move", "patch", "pin", "post", "publish", "push", "put", "react", "remove", "rename", "reopen",
    "replace", "reply", "request", "resolve", "restore", "run", "save", "schedule", "send", "set", "start", "stop",
    "submit", "tag", "transition", "trigger", "unlink", "unpin", "untag", "update", "upload", "write"}
READ_VERBS = {
    "browse", "check", "count", "describe", "explore", "fetch", "filter", "find", "get", "guide", "history", "list",
    "load", "lookup", "query", "read", "retrieve", "search", "select", "show", "status", "view"}
TARGET_KEYS = ("trigger_id", "id", "task_id", "list_id", "doc_id", "url", "owner", "repo", "pullNumber",
               "issue_number", "number", "path", "file_path", "name", "title", "collection", "channel")
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
SCHEDULE_TOOLS = {"ScheduleWakeup", "CronCreate", "CronDelete"}
REMOTE_WRITE_ACTIONS = {"create", "update", "run", "create_webhook_trigger", "delete"}
ARTIFACT_READ_ACTIONS = {"read", "list", "get", "query", "open", "quickstart", "watch"}
GIT_READ_ONLY = {"status", "log", "diff", "show", "rev-parse", "rev-list", "merge-base", "ls-files",
                 "ls-remote", "ls-tree", "describe", "blame", "grep", "shortlog", "for-each-ref", "cat-file",
                 "check-ignore", "version", "help", "count-objects", "name-rev", "var", "archive",
                 "merge-tree", "format-patch", "range-diff", "whatchanged", "show-ref", "verify-commit",
                 "diff-tree", "diff-index", "diff-files", "check-attr", "check-ref-format", "show-branch",
                 "cherry", "fsck", "verify-tag", "get-tar-commit-id"}
GIT_WRITES = {"commit", "push", "pull", "rebase", "merge", "reset", "cherry-pick", "revert", "clean", "rm", "mv",
              "restore", "am", "apply", "init", "clone", "switch", "checkout", "gc", "prune", "update-ref", "add",
              "filter-branch", "filter-repo", "fast-import", "rerere"}
GIT_SUBCOMMAND_WRITES = {
    "notes": {"add", "append", "copy", "edit", "merge", "remove", "prune"},
    "sparse-checkout": {"set", "add", "init", "disable", "reapply"},
    "submodule": {"add", "update", "init", "deinit", "sync", "absorbgitdirs", "set-branch", "set-url"},
    "bisect": {"start", "good", "bad", "new", "old", "skip", "reset", "replay", "run"},
    "lfs": {"track", "untrack", "install", "uninstall", "push", "pull", "fetch", "checkout", "migrate", "lock",
            "unlock", "prune"},
    "bundle": {"unbundle"},
}
GIT_SUBCOMMAND_READS = {"notes": {"show", "list", "get-ref"}, "sparse-checkout": {"list"},
                        "bundle": {"create", "verify", "list-heads"},
                        "submodule": {"status", "summary", "foreach"}, "bisect": {"log", "visualize", "view"},
                        "lfs": {"ls-files", "status", "env", "version", "logs", "locks"}}
# `git tag`, `git branch` and `git replace` list unless given a name or a flag that changes refs.
GIT_TAG_READS = {"-l", "--list", "-v", "--verify", "-n", "--contains", "--no-contains", "--merged", "--no-merged",
                 "--points-at"}
GIT_TAG_VALUES = {"-m", "--message", "-F", "--file", "-u", "--local-user", "--sort", "--format", "--cleanup",
                  "--trailer"}
GIT_BRANCH_WRITES = {"-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy", "-u", "--set-upstream-to",
                     "--unset-upstream", "--edit-description"}
GIT_BRANCH_READS = {"-l", "--list", "-a", "--all", "-r", "--remotes", "--show-current", "--contains", "--no-contains",
                    "--merged", "--no-merged", "--points-at"}
GIT_FETCH_VALUES = {"--depth", "--deepen", "--shallow-since", "--shallow-exclude", "-j", "--jobs", "--upload-pack",
                    "--negotiation-tip", "-o", "--server-option", "--filter", "--refmap"}
GH_READ_GROUPS = {"auth", "config", "help", "version", "search", "browse", "status"}  # after GH_WRITES
GH_READ_VERBS = {"view", "list", "status", "checks", "diff", "search", "browse", "watch", "download", "verify"}
GH_VALUE_FLAGS = {"-R", "--repo", "-b", "--body", "-F", "--body-file", "-t", "--title", "-B", "--base", "-H", "--head",
                  "-l", "--label", "-a", "--assignee", "-m", "--milestone", "-r", "--reviewer", "-p", "--project",
                  "-c", "--comment", "--reason", "-s", "--state", "-L", "--limit", "-q", "--jq", "-T", "--template",
                  "--json", "-S", "--search", "-A", "--author", "--subject", "--match-head-commit", "--add-label",
                  "--remove-label", "--add-assignee", "--remove-assignee", "--add-reviewer", "--remove-reviewer",
                  "--add-project", "--remove-project", "--duplicate-of"}
GH_WRITES = {
    "pr": {"create", "merge", "close", "reopen", "comment", "edit", "review", "ready", "lock", "unlock"},
    "issue": {"create", "close", "reopen", "comment", "edit", "delete", "transfer", "pin", "unpin", "lock", "unlock",
              "develop"},
    "release": {"create", "delete", "edit", "upload"},
    "repo": {"create", "delete", "edit", "fork", "rename", "archive", "sync"},
    "label": {"create", "delete", "edit", "clone"},
    "workflow": {"run", "enable", "disable"},
    "run": {"rerun", "cancel", "delete"},
    "secret": {"set", "delete"},
    "gist": {"create", "edit", "delete", "rename"},
    "variable": {"set", "delete"},
    "auth": {"login", "logout", "refresh", "setup-git", "switch"},
    "config": {"set", "clear-cache"},
}


def _mask(match: re.Match) -> str:
    return (match.group(1) or "") + "[masked]"


def _short(text, limit: int = MAX_TEXT) -> str:
    text = SECRET.sub(_mask, " ".join(str(text).split()))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _masked(value):
    """The output with credential shapes masked in every string, paths and keys included.

    Paths are kept raw until here: git roots are found from the real directories.
    """
    if isinstance(value, str):
        return SECRET.sub(_mask, value)
    if isinstance(value, dict):
        return {_masked(key): _masked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item) for item in value]
    return value


def _first_line(text: str) -> str:
    text = text.strip()
    return _short(text.splitlines()[0] if text else "", MAX_COMMAND)


def _content(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _text(value) -> str:
    """Text of a tool result or message, which is a string or a list of content blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    return ""


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:  # a quote opened on an earlier line: keep quoted words together anyway
        return [re.sub(r"[\"']", "", token) for token in re.findall(r"(?:[^\s'\"]|'[^']*'|\"[^\"]*\")+|['\"]", segment)
                if token not in ("'", '"')]


def _command_tokens(segment: str) -> list[str]:
    """Tokens of a simple command from its program on, which is reduced to its name."""
    tokens = _tokens(segment)
    while tokens and (tokens[0] in COMMAND_PREFIXES or ASSIGNMENT.match(tokens[0])):
        tokens = tokens[1:]  # keywords, wrappers and environment assignments before the program
    if tokens and tokens[0].startswith("/"):
        tokens[0] = os.path.basename(tokens[0])
    return tokens


def _heredoc_body(command: str, index: int, delimiter: str, tabs: bool) -> tuple[str, int]:
    """The body of a heredoc that starts at ``index`` and the index after its delimiter line.

    Without a delimiter line the body runs to the end of the input, as bash and zsh read it.
    """
    lines, length = [], len(command)
    while index < length:
        end = command.find("\n", index)
        end = length if end < 0 else end
        line = command[index:end]
        index = end + 1
        if (line.lstrip("\t") if tabs else line).rstrip("\r") == delimiter:
            break
        lines.append(line)
    return "\n".join(lines), min(index, length)


def _expansions(body: str, depth: int) -> list[str]:
    """Commands that an unquoted heredoc body runs: its `$(…)` and backquoted substitutions.
    The rest of the body is data, and an escaped `\\$(` stays text."""
    segments, index, length = [], 0, len(body)
    if depth >= MAX_DEPTH:
        return segments
    while True:
        match = BODY_EXPANSION.search(body, index)
        if not match:
            return segments
        index = match.start()
        if match.group() == "\\":
            index += 2
        elif match.group() == "`":
            end = body.find("`", index + 1)
            end = length if end < 0 else end
            segments.extend(_segments(body[index + 1:end]))
            index = end + 1
        else:
            inner, index = _parse(body, index + 2, depth + 1)
            segments.extend(inner)


def _shell_input(tokens: list[str]) -> tuple[str, str | None]:
    """Where a shell takes its commands from: ("script", text) for -c, ("stdin", None) or ("file", None)."""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in ("-o", "+o"):
            index += 2
            continue
        if len(token) > 1 and token[0] in "-+" and not token.startswith("--"):
            if "c" in token[1:]:
                return ("script", tokens[index + 1]) if index + 1 < len(tokens) else ("file", None)
            if "s" in token[1:]:
                return "stdin", None
        elif not token.startswith("-"):
            return "file", None
        index += 1
    return "stdin", None


def _segments(command: str) -> list[str]:
    """Simple commands of a shell command line, split at unquoted operators and line ends.

    Command substitutions become segments of their own and `$(…)` in the command around
    them. A heredoc body is data unless a shell reads it, as in `bash <<'EOF'`, but the
    substitutions of an unquoted body (`<<EOF`) run.
    """
    return _parse(command, 0, 0)[0]


def _parse(command: str, index: int, depth: int) -> tuple[list[str], int]:
    """Segments from ``index`` to the end, or to the `)` that closes a nested parse (``depth`` > 0).

    A `<<` inside parentheses that close on the same line, as in `$((1<<2))`, opens no heredoc.
    """
    segments, current, heredocs = [], [], []
    quote, length = None, len(command)

    def add(text: str) -> None:
        if text:
            current.append(text)

    def flush() -> None:
        segment = "".join(current).strip()
        if segment:
            segments.append(segment)
        current.clear()

    def backquoted(start: int) -> int:
        end = command.find("`", start + 1)
        end = length if end < 0 else end
        segments.extend(_segments(command[start + 1:end]))
        current.append("`…`")
        return end + 1

    while index < length:
        if quote:
            match = QUOTE_END[quote].search(command, index)
            if not match:
                add(command[index:])
                break
            add(command[index:match.start()])
            index, found = match.start(), match.group()
            if found == "$(" and depth < MAX_DEPTH:  # a substitution inside "..." runs too
                inner, index = _parse(command, index + 2, depth + 1)
                segments.extend(inner)
                current.append("$(…)")
            elif found == "`":
                index = backquoted(index)
            elif found == "$(":
                current.append(found)
                index += 2
            elif found == "\\":  # an escape inside "..." or $'...'
                if command.startswith("\n", index + 1):
                    index += 2  # a line continuation
                else:
                    current.append(command[index:index + 2])
                    index += 2
            else:
                current.append(found)
                quote, index = None, index + 1
            continue
        match = SHELL_SPECIAL.search(command, index)
        if not match:
            add(command[index:])
            break
        add(command[index:match.start()])
        index, char = match.start(), match.group()
        word_start = not "".join(current[-1:])[-1:].strip()
        if char == "\\":
            if command.startswith("\n", index + 1) or command.startswith("\r\n", index + 1):
                index += 2 if command[index + 1] == "\n" else 3  # a line continuation
            else:
                current.append(command[index:index + 2])
                index += 2
        elif char in "'\"":
            quote = "$'" if char == "'" and command[index - 1:index] == "$" else char
            current.append(char)
            index += 1
        elif char == "#" and word_start:  # a comment runs to the end of the line
            end = command.find("\n", index)
            index = length if end < 0 else end
        elif char == "<" and command.startswith("<<", index) and not command.startswith("<<<", index):
            heredoc = HEREDOC.match(command, index)
            text = heredoc.group() if heredoc else "<<"
            if heredoc:
                single, double, escaped, word = heredoc.group(2, 3, 4, 5)
                delimiter = next(part for part in (single, double, word) if part is not None)
                quoted = word is None or bool(escaped)  # 'EOF', "EOF" and \EOF keep the body text
                heredocs.append((delimiter, heredoc.group(1) == "-", "".join(current), quoted))
            current.append(text)
            index += len(text)
        elif char == "<":
            text = "<<<" if command.startswith("<<<", index) else "<"
            current.append(text)
            index += len(text)
        elif char == "\n":
            flush()
            index += 1
            for delimiter, tabs, owner, quoted in heredocs:
                body, index = _heredoc_body(command, index, delimiter, tabs)
                tokens = _command_tokens(owner)
                if tokens and tokens[0] in SHELLS and _shell_input(tokens)[0] == "stdin":
                    segments.extend(_segments(body))
                elif not quoted:
                    segments.extend(_expansions(body, depth))
            heredocs = []
        elif char == "&" and (command[index - 1:index] in (">", "<") or command.startswith(">", index + 1)):
            current.append(char)  # a redirection such as 2>&1 or &>file
            index += 1
        elif char == "(" and depth < MAX_DEPTH:
            substitution = command[index - 1:index] == "$"
            if not substitution:
                flush()  # a subshell, a group or a function's parentheses
            inner, index = _parse(command, index + 1, depth + 1)
            segments.extend(inner)
            if substitution:
                current.append("(…)")
        elif char == ")":
            flush()
            index += 1
            if depth:
                return segments, index
        elif char == "`":
            index = backquoted(index)
        else:  # ; | & and parentheses past MAX_DEPTH end a simple command
            flush()
            index += 1
    flush()
    return segments, length


def _name_words(name: str) -> list[str]:
    tail = name.rsplit("__", 1)[-1]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tail).lower()
    spaced = re.sub(r"pull[_\-\s]?requests?", "pullrequest", spaced)  # a noun, not the verb "request"
    return [word for word in re.split(r"[_\-\s]+", spaced) if word]


def _target(data) -> str:
    """The tool's target identifiers, never its full input."""
    if not isinstance(data, dict):
        return ""
    parts = [f"{key}={_short(data[key], 80)}" for key in TARGET_KEYS
             if isinstance(data.get(key), (str, int)) and str(data[key])]
    return " ".join(parts[:4])


def classify_tool(name: str, data) -> str:
    """write, read, logging or unclassified, for tools other than files and shell."""
    data = data if isinstance(data, dict) else {}
    action = data.get("action")
    if name.endswith("log_interaction"):
        return "logging"
    if name == "RemoteTrigger":
        return "write" if action in REMOTE_WRITE_ACTIONS else "read"
    if name == "Artifact":
        return "read" if (action or "publish") in ARTIFACT_READ_ACTIONS else "write"
    if name in ("ArtifactData", "ArtifactComments"):
        return "read" if not action or action in ARTIFACT_READ_ACTIONS else "write"
    if not name.startswith("mcp__"):
        return "read"
    words = _name_words(name)
    if any(word in WRITE_VERBS for word in words):
        return "write"
    if any(word in READ_VERBS for word in words):
        return "read"
    return "unclassified"


def git_kind(tokens: list[str]) -> str:
    """write, read or unknown for a tokenised `git ...` command."""
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in ("-C", "-c") else 1
    if index >= len(tokens):
        return "read"
    sub, args = tokens[index], tokens[index + 1:]
    first = args[0] if args else ""
    if sub in GIT_READ_ONLY:
        return "read"
    if sub in GIT_WRITES:
        return "write"
    if sub in GIT_SUBCOMMAND_WRITES:
        if first in GIT_SUBCOMMAND_WRITES[sub]:
            return "write"
        return "read" if not first or first in GIT_SUBCOMMAND_READS.get(sub, ()) else "unknown"
    if sub == "stash":
        return "read" if first in ("list", "show") else "write"
    if sub == "tag":
        return _list_or_create(args, {"-d", "--delete"}, GIT_TAG_READS, GIT_TAG_VALUES)
    if sub == "worktree":
        return "write" if first in ("add", "remove", "prune", "move", "repair", "lock", "unlock") else "read"
    if sub == "branch":
        return _list_or_create(args, GIT_BRANCH_WRITES, GIT_BRANCH_READS, {"--sort", "--format"})
    if sub == "remote":
        return "write" if first in ("add", "remove", "rm", "rename", "set-url", "set-head", "set-branches",
                                    "prune") else "read"
    if sub == "reflog":
        return "write" if first in ("expire", "delete", "drop", "write") else "read"
    if sub == "replace":
        return _list_or_create(args, {"-d", "--delete", "--edit", "--graft", "--convert-graft-file"},
                               {"-l", "--list"}, {"--format"})
    if sub == "config":
        return _git_config_kind(args)
    if sub == "fetch":
        return _git_fetch_kind(args)
    if sub == "hash-object":
        return "write" if "-w" in args else "read"
    if sub == "symbolic-ref":
        return "write" if len([a for a in args if not a.startswith("-")]) >= 2 else "read"
    return "unknown"


def _git_config_kind(args: list[str]) -> str:
    first = args[0] if args else ""
    if first in ("set", "unset", "rename-section", "remove-section", "edit"):
        return "write"  # subcommands since Git 2.46
    if first in ("get", "list"):
        return "read"
    if any(arg in ("--unset", "--unset-all", "--remove-section", "--rename-section", "--edit", "-e", "--add",
                   "--replace-all") for arg in args):
        return "write"
    reads = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--get-color", "--get-colorbool", "--list",
             "-l", "--show-origin", "--show-scope"}
    if any(arg in reads for arg in args):
        return "read"
    values = _positional(args, {"-f", "--file", "--blob", "--type", "--default", "--comment"})
    return "write" if len(values) >= 2 else "read"  # `git config <key> <value>` sets


def _git_fetch_kind(args: list[str]) -> str:
    """A fetch reads: it updates remote-tracking refs, which mirror the remote. It writes when a
    refspec names a local destination, as `git fetch origin pull/1/head:pr-1` creates a branch."""
    if "--dry-run" in args:
        return "read"
    for spec in _positional(args, GIT_FETCH_VALUES)[1:]:  # the first names the repository
        destination = spec.split(":", 1)[1] if ":" in spec else ""
        if destination and not destination.startswith("refs/remotes/"):
            return "write"
    return "read"


def _list_or_create(args: list[str], writes: set, reads: set, values: set) -> str:
    """`git tag`, `git branch` or `git replace`: a flag in ``writes`` changes refs, one in ``reads``
    lists or verifies, and otherwise a name creates while options alone list."""
    for arg in args:
        flag = arg.split("=", 1)[0]
        if flag in writes:
            return "write"
        if flag.rstrip("0123456789") in reads:  # -n5 is -n with its value
            return "read"
    return "write" if _positional(args, values) else "read"


def _positional(args: list[str], values: set) -> list[str]:
    """Arguments that are not options, skipping the value of each option in ``values``."""
    found, skip = [], False
    for arg in args:
        if skip:
            skip = False
        elif arg in values:
            skip = True
        elif not arg.startswith("-"):
            found.append(arg)
    return found


def git_write(tokens: list[str]) -> bool:
    """Whether a tokenised `git ...` command changes a repository or its configuration."""
    return git_kind(tokens) == "write"


def gh_kind(tokens: list[str], segment: str) -> str:
    """write, read or unknown for a tokenised `gh ...` command."""
    if len(tokens) < 2:
        return "read"
    group, verb = tokens[1], tokens[2] if len(tokens) > 2 else ""
    if group == "api":
        return "write" if gh_write(tokens, segment) else "read"
    if group in GH_WRITES and verb in GH_WRITES[group]:
        return "write"
    if verb in GH_READ_VERBS or group in GH_READ_GROUPS or not verb:
        return "read"
    return "unknown"


def gh_write(tokens: list[str], segment: str) -> bool:
    """Whether a tokenised `gh ...` command writes to GitHub."""
    if len(tokens) < 2:
        return False
    group, verb = tokens[1], tokens[2] if len(tokens) > 2 else ""
    if group in GH_WRITES:
        return verb in GH_WRITES[group]
    if group != "api":
        return False
    for index, token in enumerate(tokens):
        method = None
        if token in ("-X", "--method") and index + 1 < len(tokens):
            method = tokens[index + 1]
        elif token.startswith("--method="):
            method = token.split("=", 1)[1]
        elif token.startswith("-X") and len(token) > 2:
            method = token[2:]
        if method:
            return method.upper() != "GET"
    if "graphql" in tokens:
        return "mutation" in segment
    return any(token in ("-f", "-F", "--field", "--raw-field", "--input") for token in tokens)


def _resolve(path: str, cwd: str | None) -> str | None:
    path = os.path.expanduser(path)
    if not path.startswith("/"):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    return os.path.normpath(path)


def _earlier(ts, than) -> bool:
    """Whether ``ts`` comes before ``than``. Transcripts stamp entries in ISO 8601 UTC, which sorts as text."""
    return bool(ts) and (not than or ts < than)


def _add_ref(refs: dict, repository, kind: str, number: int, ts) -> None:
    key = (repository, kind, number)
    ref = refs.get(key)
    if ref is None:
        url = f"https://github.com/{repository}/{kind}/{number}" if repository and kind != "ref" else None
        refs[key] = {"repository": repository, "kind": kind, "number": number, "url": url, "first_seen": ts}
    elif _earlier(ts, ref["first_seen"]):
        ref["first_seen"] = ts  # subagent transcripts are read after the main one


def _scan_refs(text: str, refs: dict, ts) -> None:
    for match in GITHUB_URL.finditer(text):
        _add_ref(refs, match.group(1), match.group(2), int(match.group(3)), ts)
    for match in SHORT_REF.finditer(text):
        _add_ref(refs, match.group(1), "ref", int(match.group(2)), ts)


def _notices(text: str, ts, ended: dict) -> None:
    """Every task id of a notification block gets that block's status."""
    for block in NOTICE.findall(text):
        status = STATUS.search(block)
        for task_id in TASK_ID.findall(block):
            ended[task_id] = {"status": status.group(1) if status else "notified", "ts": ts}


def _git_paths(tokens: list[str], current: str | None, ts, sink: dict) -> None:
    for index, token in enumerate(tokens[:-1]):
        if token == "-C":
            path = _resolve(tokens[index + 1], current)
            if path:
                sink["directories"].setdefault(path, ts)
                current = path  # git resolves the remaining relative paths from -C
    if "worktree" in tokens and "add" in tokens:
        names = _positional(tokens[tokens.index("add") + 1:], {"-b", "-B", "--reason"})
        path = _resolve(names[0], current) if names else None
        if path:
            sink["directories"].setdefault(path, ts)


def _shell(command: str, cwd: str | None, ts, sink: dict, actor: str) -> None:
    """Record writes, directories, references and scratchpad paths of one shell command."""
    current = cwd
    pending = _segments(command)[::-1]
    while pending:
        segment = pending.pop()
        _scan_refs(segment, sink["refs"], ts)
        tokens = _command_tokens(segment)
        if not tokens:
            continue
        program = tokens[0]
        source, script = _shell_input(tokens) if program in SHELLS else (None, None)
        if source == "script" and script:
            pending.extend(_segments(script)[::-1])  # `bash -c '<script>'` runs the script's commands
        elif program == "cd" and len(tokens) > 1:
            current = _resolve(tokens[1], current)
            if current:
                sink["directories"].setdefault(current, ts)
        elif program in ("git", "gh"):
            if program == "git":
                _git_paths(tokens, current, ts, sink)
                kind = git_kind(tokens)
            else:
                kind = gh_kind(tokens, segment)
                _gh_ref(tokens, ts, sink)
            shown = _first_line(KEYWORD_PREFIX.sub("", segment))
            if kind == "write":
                sink["git_mutations"].append({"ts": ts, "command": shown, "by": actor})
            elif kind == "unknown":
                sink["unclassified_commands"].append({"ts": ts, "command": shown, "by": actor})
    for token in TOKEN_SPLIT.split(command):  # heredoc bodies too: a script there may write the file
        if token.startswith("/") and "/scratchpad" in token:
            sink["scratch"].setdefault(token.rstrip(".,:"), ts)


def _gh_ref(tokens: list[str], ts, sink: dict) -> None:
    """The pull request or issue a `gh pr|issue <verb>` command names, with its repository."""
    if len(tokens) < 4 or tokens[1] not in ("pr", "issue"):
        return
    repo, positional, skip = None, None, False
    for index, token in enumerate(tokens[3:], start=3):
        if skip:
            skip = False
            continue
        if token.startswith("--repo="):
            repo = token.split("=", 1)[1]
        elif token in ("-R", "--repo"):
            repo = tokens[index + 1] if index + 1 < len(tokens) else None
            skip = True
        elif token in GH_VALUE_FLAGS:
            skip = True
        elif token.startswith("-"):
            continue  # a boolean flag, or a value given with `=`
        elif positional is None:
            positional = token
    if positional and positional.lstrip("#").isdigit():
        _add_ref(sink["refs"], repo, "pull" if tokens[1] == "pr" else "issues", int(positional.lstrip("#")), ts)


def load_entries(paths: list[Path]) -> tuple[list[dict], int]:
    """Entries of all transcript parts in file order, and the count of unreadable lines."""
    entries, bad = [], 0
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries, bad


def _file(sink: dict, path: str, ts, actor: str) -> None:
    """Every writer of a path, with the time of its first write there."""
    writers = sink["files"].setdefault(path, {})
    if actor not in writers or _earlier(ts, writers[actor]):
        writers[actor] = ts


def _writers(path: str, writers: dict) -> dict:
    """A written file with its first write's time and its writers in the order they started writing."""
    order = sorted(writers, key=lambda actor: writers[actor] or "")
    return {"path": path, "ts": writers[order[0]], "by": order}


def _in_time_order(items: list[dict]) -> list[dict]:
    """Items of the main and subagent transcripts in one chronological list; ties keep their order."""
    return sorted(items, key=lambda item: item.get("ts") or "")


def _new_sink() -> dict:
    return {"files": {}, "memory": {}, "git_mutations": [], "external": [], "scheduled": [], "unclassified": Counter(),
            "logging": 0, "scratch": {}, "directories": {}, "refs": {}, "started": [], "ended": {}, "tools": {},
            "corrections": [], "prompts": [], "user_commands": [], "models": Counter(), "tokens": Counter(),
            "unclassified_commands": [], "file_events": Counter(),
            "responses": set(), "sessions": [], "bridges": [], "titles": [], "first": None, "last": None}


def scan(entries: list[dict], sink: dict | None = None, actor: str = "main") -> dict:
    """Accumulate one transcript's signals into ``sink`` without touching the file system."""
    sink = sink if sink is not None else _new_sink()
    for entry in entries:
        kind, ts, cwd = entry.get("type"), entry.get("timestamp"), entry.get("cwd")
        if ts and actor == "main":
            sink["first"] = sink["first"] or ts
            sink["last"] = ts
        if actor == "main" and entry.get("sessionId") and entry["sessionId"] not in sink["sessions"]:
            sink["sessions"].append(entry["sessionId"])
        if cwd:
            sink["directories"].setdefault(cwd, ts)
        if kind == "bridge-session" and entry.get("bridgeSessionId") and entry["bridgeSessionId"] not in sink["bridges"]:
            sink["bridges"].append(entry["bridgeSessionId"])
        elif kind == "ai-title" and entry.get("aiTitle"):
            sink["titles"].append(_short(entry["aiTitle"], 200))
        elif kind == "pr-link" and entry.get("prNumber"):
            _add_ref(sink["refs"], entry.get("prRepository"), "pull", int(entry["prNumber"]), ts)
        elif kind == "file-history-delta" and entry.get("trackingPath"):
            parent = (entry.get("backup") or {}).get("realParentDir")
            path = entry["trackingPath"]
            full = path if path.startswith("/") or not parent else str(Path(parent) / Path(path).name)
            _file(sink, full, ts, actor)

        top = entry.get("content")
        if isinstance(top, str) and "<task-id>" in top:
            _notices(top, ts, sink["ended"])
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        if kind == "user":
            _user(entry, message, ts, cwd, sink, actor)
        elif kind == "assistant":
            _assistant(entry, message, ts, cwd, sink, actor)
    return sink


def _assistant(entry: dict, message: dict, ts, cwd, sink: dict, actor: str) -> None:
    key = message.get("id") or entry.get("requestId") or entry.get("uuid")
    if key not in sink["responses"]:
        sink["responses"].add(key)
        sink["models"][message.get("model") or "unknown"] += 1
        usage = message.get("usage") or {}
        for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            sink["tokens"][field] += usage.get(field) or 0
    for block in _content(entry):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text") or ""
            for match in CORRECTION.finditer(text):
                start = max(0, match.start() - 160)
                sink["corrections"].append({"ts": ts, "by": actor, "snippet": _short(text[start:match.end() + 200])})
        elif block.get("type") == "tool_use":
            _tool_use(block, ts, cwd, sink, actor)


def _prompt_text(content) -> str:
    """A prompt's text, with a marker for each image or other non-text block."""
    if not isinstance(content, list):
        return _text(content)
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, dict) and block.get("type") != "tool_result":
            parts.append(f"[{block.get('type') or 'attachment'}]")
    return " ".join(part for part in parts if part)


def _user(entry: dict, message: dict, ts, cwd, sink: dict, actor: str) -> None:
    content = message.get("content")
    text = _text(content)
    if "<task-id>" in text:
        _notices(text, ts, sink["ended"])
    for block in content if isinstance(content, list) else []:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            result = _text(block.get("content"))
            _scan_refs(result, sink["refs"], ts)
            tool = sink["tools"].get(block.get("tool_use_id"), {})
            if tool.get("tool") == "TaskStop":
                for task_id in STOPPED.findall(result):
                    sink["ended"][task_id] = {"status": "stopped", "ts": ts}
            started = STARTED.get(tool.get("tool"))
            match = started.match(result) if started else None
            if match:
                sink["started"].append({**tool, "id": match.group(1), "by": actor})
    if actor != "main" or entry.get("isMeta") or entry.get("isCompactSummary"):
        return
    origin = entry.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else None
    stripped = text.strip()
    if stripped.startswith("<bash-input>"):
        command = re.sub(r"</?bash-input>", "", stripped)
        sink["user_commands"].append({"ts": ts, "command": _first_line(command)})
        _shell(command, cwd, ts, sink, "user")
        return
    if origin_kind:
        is_prompt = origin_kind == "human"
    else:  # transcripts without `origin`: a plain string that is not a harness tag
        is_prompt = isinstance(content, str) and not stripped.startswith("<")
    prompt = _prompt_text(content).strip()
    if is_prompt and prompt:
        sink["prompts"].append({"ts": ts, "text": _short(prompt)})
        _scan_refs(prompt, sink["refs"], ts)


def _tool_use(block: dict, ts, cwd, sink: dict, actor: str) -> None:
    name, data = block.get("name") or "", block.get("input") or {}
    data = data if isinstance(data, dict) else {}
    description = data.get("description") or data.get("name") or data.get("prompt") or ""
    sink["tools"][block.get("id")] = {"tool": name, "ts": ts, "description": _short(description, 160)}
    if name in WRITE_TOOLS:
        path = data.get("file_path") or data.get("notebook_path")
        if path:
            _file(sink, path, ts, actor)
            sink["file_events"][actor] += 1
            if MEMORY_PATH.search(path):
                if path not in sink["memory"] or _earlier(ts, sink["memory"][path]["ts"]):
                    sink["memory"][path] = {"ts": ts, "by": actor}
            if "/scratchpad" in path:
                sink["scratch"].setdefault(path, ts)
    elif name == "Bash":
        _shell(data.get("command") or "", cwd, ts, sink, actor)
    elif name in SCHEDULE_TOOLS:
        sink["scheduled"].append({"ts": ts, "tool": name, "by": actor,
                                  "summary": _short(data.get("reason") or data.get("prompt") or "", 160)})
    else:
        kind = classify_tool(name, data)
        if kind == "write":
            sink["external"].append({"ts": ts, "tool": name, "by": actor,
                                     "summary": " ".join(filter(None, [data.get("action"), _target(data)]))})
        elif kind == "logging":
            sink["logging"] += 1
        elif kind == "unclassified":
            sink["unclassified"][name] += 1
        if name.startswith("mcp__"):
            _structured_ref(name, data, ts, sink)
    if name not in WRITE_TOOLS and name != "Bash":  # file contents are not references; _shell scans commands
        _scan_refs("\n".join(_strings(data)), sink["refs"], ts)


def _strings(value) -> list[str]:
    """Every string inside a tool input."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _structured_ref(name: str, data: dict, ts, sink: dict) -> None:
    """References given as owner, repo and a number, as GitHub MCP tools take them."""
    owner, repo = data.get("owner"), data.get("repo")
    if not (isinstance(owner, str) and isinstance(repo, str)):
        return
    lowered = name.lower()
    for key, kind in (("pullNumber", "pull"), ("pull_number", "pull"), ("issue_number", "issues"),
                      ("issueNumber", "issues"), ("number", "pull" if "pull" in lowered else "issues")):
        value = data.get(key)
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            _add_ref(sink["refs"], f"{owner}/{repo}", kind, int(value), ts)
            return


def summarize(sink: dict) -> dict:
    background = [{**task, "ended": sink["ended"].get(task["id"])} for task in _in_time_order(sink["started"])]
    refs = sorted(sink["refs"].values(), key=lambda ref: (ref["repository"] or "", ref["kind"], ref["number"]))
    return {
        "sessions": sink["sessions"],
        "bridge_sessions": sink["bridges"],
        "title": sink["titles"][-1] if sink["titles"] else None,
        "first": sink["first"],
        "last": sink["last"],
        "models": dict(sink["models"]),
        "responses": len(sink["responses"]),
        "tokens": dict(sink["tokens"]),
        "prompts": sink["prompts"],
        "user_commands": sink["user_commands"],
        "directories": sorted(sink["directories"]),
        "files_written": [_writers(path, writers) for path, writers in sorted(sink["files"].items())],
        "memory_writes": [{"path": path, **info} for path, info in sorted(sink["memory"].items())],
        "git_mutations": _in_time_order(sink["git_mutations"]),
        "unclassified_commands": _in_time_order(sink["unclassified_commands"]),
        "github_refs": refs,
        "external_writes": _in_time_order(sink["external"]),
        "scheduled": _in_time_order(sink["scheduled"]),
        "unclassified_tools": dict(sink["unclassified"]),
        "logging_calls": sink["logging"],
        "background_tasks": background,
        "scratchpad_paths": sorted(sink["scratch"]),
        "corrections": _in_time_order(sink["corrections"]),
    }


def inventory(entries: list[dict]) -> dict:
    """The thread's artifacts and signals from one transcript's entries."""
    return _masked(summarize(scan(entries)))


def subagent_transcripts(paths: list[Path]) -> list[Path]:
    """Transcripts of subagents and workflow agents stored next to the session's transcript."""
    found = []
    for path in paths:
        directory = path.with_suffix("")
        if directory.is_dir():
            found += sorted(p for p in directory.rglob("*.jsonl") if p.is_file())
    return found


def git_roots(directories: list[str]) -> list[str]:
    """Distinct git work trees among the directories that still exist."""
    roots = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        result = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, check=False)
        root = result.stdout.strip()
        if result.returncode == 0 and root and root not in roots:
            roots.append(root)
    return roots


def config_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    dirs += sorted(Path.home().glob(".claude*"))
    return [d for d in dict.fromkeys(dirs) if (d / "projects").is_dir()]


def find_session(session: str) -> list[Path]:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session):
        raise SystemExit("session id must be a UUID")
    return [path for base in config_dirs() for path in (base / "projects").glob(f"*/{session}.jsonl")]


def project_dir_name(directory: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(directory).resolve()))


def latest_in_project(directory: str) -> tuple[Path | None, list[str]]:
    candidates = [path for base in config_dirs()
                  for path in (base / "projects" / project_dir_name(directory)).glob("*.jsonl")]
    if not candidates:
        return None, []
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    recent = [str(path) for path in candidates[1:] if time.time() - path.stat().st_mtime < 3600]
    return candidates[0], recent


def collect(paths: list[Path]) -> dict:
    """Inventory of the transcripts and of their subagents' transcripts."""
    entries, bad = load_entries(paths)
    sink = scan(entries)
    subagents = []
    for part in subagent_transcripts(paths):
        part_entries, part_bad = load_entries([part])
        bad += part_bad
        before = (len(sink["git_mutations"]), len(sink["external"]), len(sink["responses"]))
        scan(part_entries, sink, actor=part.stem)
        counts = {"git_mutations": len(sink["git_mutations"]) - before[0],
                  "external_writes": len(sink["external"]) - before[1],
                  "files_written": sink["file_events"][part.stem],
                  "responses": len(sink["responses"]) - before[2]}
        if counts["git_mutations"] or counts["external_writes"] or counts["files_written"]:
            subagents.append({"agent": part.stem, "transcript": str(part), **counts})
    result = summarize(sink)
    result["subagents_with_writes"] = subagents
    result["subagent_transcripts"] = len(subagent_transcripts(paths))
    result["transcripts"] = [str(path) for path in paths]
    result["unreadable_lines"] = bad
    result["git_roots"] = git_roots(result["directories"])
    return _masked(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="session UUID")
    source.add_argument("--transcript", action="append", type=Path, help="transcript part, repeatable")
    source.add_argument("--project-dir", help="working directory of the project, with --latest")
    parser.add_argument("--latest", action="store_true", help="with --project-dir: the newest transcript")
    args = parser.parse_args(argv)

    notes = []
    if args.session:
        paths = find_session(args.session)
    elif args.transcript:
        paths = args.transcript
    else:
        if not args.latest:
            parser.error("--project-dir needs --latest")
        newest, recent = latest_in_project(args.project_dir)
        paths = [newest] if newest else []
        if recent:
            notes.append("other transcripts of this project changed in the last hour; confirm the session: "
                         + ", ".join(recent))
    if not paths:
        print(json.dumps({"error": "transcript not found"}))
        return 1
    result = collect(paths)
    result["notes"] = _masked(notes)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
