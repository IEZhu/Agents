"""Inspect, explicitly enable/disable, and bind the optional client adapters."""
import argparse
import json

from .config import config_path, load, write


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("enable")
    sub.add_parser("disable")
    bind = sub.add_parser("bind-cursor")
    bind.add_argument("--conversation-id", required=True)
    bind.add_argument("--window-title", required=True)
    bind.add_argument("--chat-title", required=True)
    args = parser.parse_args(argv)
    try:
        target = config_path(args.config)
    except ValueError as exc:
        parser.error(str(exc))
    settings, error = load(target)
    if error == "invalid_config" and args.command != "status":
        parser.error("existing compaction settings are invalid; repair them before changing a value")
    if args.command == "enable":
        settings["enabled"] = True
    elif args.command == "disable":
        settings["enabled"] = False
    elif args.command == "bind-cursor":
        settings["cursor_bindings"] = [item for item in settings["cursor_bindings"]
                                       if item["conversation_id"] != args.conversation_id]
        settings["cursor_bindings"].append({"conversation_id": args.conversation_id,
                                          "window_title": args.window_title, "chat_title": args.chat_title})
    if args.command != "status":
        try:
            write(target, settings)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
    print(json.dumps({"config": str(target), "error": error, **settings}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
