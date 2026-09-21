#!/usr/bin/env python3
"""Redacted, reproducible macOS process/footprint snapshot. No engine imports."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


def snapshot(installation):
    raw = subprocess.check_output(["/bin/ps", "-axo", "pid=,ppid=,rss=,etime=,command="], text=True)
    processes = {}
    for line in raw.splitlines():
        parts = line.strip().split(None, 4)
        if len(parts) != 5: continue
        pid, parent, rss, age, command = parts
        processes[int(pid)] = (int(parent), int(rss), age, command)
    # Read executable paths separately: command strings may contain shell
    # wrappers, and executable paths themselves can contain spaces.
    executables = {}
    raw = subprocess.check_output(["/bin/ps", "-axo", "pid=,comm="], text=True)
    for line in raw.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            executables[int(parts[0])] = Path(parts[1]).name
    targets = []
    for pid, (parent, rss, age, command) in processes.items():
        executable = executables.get(pid, "")
        python_process = re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable, re.IGNORECASE)
        module_server = re.search(r"(?:^|\s)-m\s+src\.server(?:\s|$)", command)
        direct = python_process and (str(installation / "src/server.py") in command or module_server)
        daemon = python_process and "-m src.daemon" in command and "serve" in command
        bridge = executable.lower() in ("node", "nodejs") and str(installation / "bridge/stdio.mjs") in command
        if not (direct or daemon or bridge): continue
        ancestors, cursor = [], parent
        for _ in range(12):
            if cursor not in processes: break
            pp, _, _, cmd = processes[cursor]
            # Report only known application categories, never command arguments.
            lowered = cmd.lower()
            for app in ("codex", "claude", "cursor", "launchd", "terminal"):
                if app in lowered and app not in ancestors: ancestors.append(app)
            if pp == cursor: break
            cursor = pp
        result = subprocess.run(["/usr/bin/vmmap", "--summary", str(pid)], capture_output=True, text=True)
        match = re.search(r"Physical footprint:\s+([\d.]+)([KMGT])", result.stdout)
        footprint = int(float(match[1]) * 1024 ** ("KMGT".index(match[2]) + 1)) if match else None
        targets.append({"pid": pid, "parent_pid": parent, "parent_apps": ancestors, "age": age,
                        "rss_bytes": rss * 1024, "physical_footprint_bytes": footprint,
                        "kind": "stdio" if direct else "daemon" if daemon else "bridge"})
    swap = subprocess.check_output(["/usr/sbin/sysctl", "-n", "vm.swapusage"], text=True).strip()
    return {"timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": "intfloat/multilingual-e5-large", "processes": targets,
            "model_processes": sum(p["kind"] != "bridge" for p in targets),
            "total_physical_footprint_bytes": sum(p["physical_footprint_bytes"] or 0 for p in targets),
            "missing_footprint_count": sum(p["physical_footprint_bytes"] is None for p in targets),
            "swap": swap}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = snapshot(args.installation.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + args.output.name, dir=args.output.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(result, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, args.output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({k: v for k, v in result.items() if k != "processes"}, indent=2))
