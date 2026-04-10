"""Inject managed section into global CLAUDE.md.

Usage: python inject_claude_md.py <target_md_path> <source_md_path>

Creates or updates a managed section delimited by markers.
"""
import os
import sys

MARKER_BEGIN = "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
MARKER_END = "# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"


def main():
    md_path = sys.argv[1]
    src_path = sys.argv[2]

    with open(src_path, "r", encoding="utf-8") as f:
        section = f.read()

    new_block = f"{MARKER_BEGIN}\n\n{section}\n\n{MARKER_END}\n"

    if os.path.exists(md_path):
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()

        if MARKER_BEGIN in content and MARKER_END in content:
            bi = content.find(MARKER_BEGIN)
            ei = content.find(MARKER_END, bi) + len(MARKER_END)
            # Consume trailing newlines after marker
            while ei < len(content) and content[ei] == "\n":
                ei += 1
            content = content[:bi] + new_block + content[ei:]
            print("Replaced existing section")
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
