"""Merge missing keys from env.example into .env.

Usage: python merge_env.py <env_file> <env_example>
"""
import os
import sys


def main():
    env_file = sys.argv[1]
    env_example = sys.argv[2]

    if not os.path.exists(env_example):
        print("  env.example not found, skipping")
        return

    existing = set()
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                existing.add(s.split("=", 1)[0].strip())

    added = []
    with open(env_example, "r", encoding="utf-8") as f:
        lines = f.readlines()

    with open(env_file, "a", encoding="utf-8") as out:
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                continue
            key = s.split("=", 1)[0].strip()
            if key not in existing:
                out.write(line)
                added.append(key)

    if added:
        print(f'  Added {len(added)} missing keys: {" ".join(added)}')
    else:
        print("  All required keys present in .env")


if __name__ == "__main__":
    main()
