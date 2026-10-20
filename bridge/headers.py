"""Print private fixed HTTP headers for Codex without requiring Node."""
import json
import sys

from stdio import load_config


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("Private bridge config required")
        config, _ = load_config(sys.argv[1])
        print(json.dumps(config["headers"], ensure_ascii=True, allow_nan=False))
    except (OSError, ValueError, RecursionError):
        print("Agents-Core: invalid private HTTP header configuration.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
