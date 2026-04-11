"""Inject managed section into global CLAUDE.md.

Usage: python inject_claude_md.py <target_md_path> <source_md_path>

Creates or updates a managed section delimited by markers.
"""
import os
import sys

MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>"
MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <target_md_path> <source_md_path>", file=sys.stderr)
        sys.exit(1)

    md_path = sys.argv[1]
    src_path = sys.argv[2]

    with open(src_path, "r", encoding="utf-8") as f:
        section = f.read()

    new_block = f"{MARKER_BEGIN}\n\n{section}\n\n{MARKER_END}\n"

    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        begin_count = content.count(MARKER_BEGIN)
        end_count = content.count(MARKER_END)

        if begin_count == 1 and end_count == 1:
            bi = content.find(MARKER_BEGIN)
            ei = content.find(MARKER_END, bi)
            if ei < bi:
                print(f"ERROR: end marker appears before begin marker in {md_path}", file=sys.stderr)
                sys.exit(1)
            ei += len(MARKER_END)
            # Consume trailing newlines after marker
            while ei < len(content) and content[ei] == "\n":
                ei += 1
            content = content[:bi] + new_block + content[ei:]
            print("Replaced existing section")
        elif begin_count > 0 or end_count > 0:
            print(f"ERROR: expected exactly 1 begin and 1 end marker, found {begin_count} begin and {end_count} end", file=sys.stderr)
            print(f"Please fix markers in {md_path} manually", file=sys.stderr)
            sys.exit(1)
        else:
            content = content + "\n" + new_block
            print("Appended section")
    else:
        content = new_block
        print("Created new file")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)


if __name__ == "__main__":
    main()
