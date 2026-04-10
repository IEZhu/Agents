#!/usr/bin/env python3
import os
import re
import sys

# Configuration
TARGET_EXTENSIONS = {
    '.py', '.md', '.mdc', '.txt', '.json', '.html',
    '.js', '.ts', '.css', '.scss', '.yaml', '.yml',
    '.sh', '.bash', '.zsh', '.env', '.example'
}
IGNORE_DIRS = {
    '.git', '.venv', '__pycache__', 'node_modules',
    'chroma_db', 'data', 'dist', 'build',
    '.pytest_cache', '.mypy_cache', 'logs'
}

def is_text_file(filepath):
    """Check if file has a target extension."""
    return os.path.splitext(filepath)[1] in TARGET_EXTENSIONS or os.path.basename(filepath) in ['requirements.txt', '.gitignore', '.editorconfig', 'Dockerfile']

def clean_file(filepath):
    """
    Cleans up a file:
    1. Removes trailing whitespace from end of lines (optional, but good).
    2. Ensures exactly one newline at EOF.
    3. Collapses 3+ consecutive newlines to 2.
    """
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        print(f"Skipping binary or non-utf8 file: {filepath}")
        return False
    except Exception as e:
        print(f"Error reading {filepath}: {e}")
        return False

    original_content = content

    # 1. Normalize line endings (optional, usually handled by git, but good for processing)
    # content = content.replace('\r\n', '\n')

    # 2. Collapse 3+ newlines to 2
    content = re.sub(r'\n{3,}', '\n\n', content)

    # 3. Ensure exactly one newline at EOF
    content = content.rstrip() + '\n'

    # 4. (Optional) Trim trailing whitespace on lines?
    # Let's keep it simple: just fix the "excessive newlines" issue user mentioned.
    # User said "problem with extra newlines" (plural), likely at EOF or between blocks.

    if content != original_content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Fixed: {filepath}")
        return True
    return False

def main():
    root_dir = os.getcwd()
    changed_files = 0

    # Walk the directory
    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Filter directories in-place
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

        for filename in filenames:
            filepath = os.path.join(dirpath, filename)

            if is_text_file(filepath):
                if clean_file(filepath):
                    changed_files += 1

    if changed_files > 0:
        print(f"\nCleaned {changed_files} files.")
        sys.exit(1) # Return non-zero to indicate changes occurred (useful for pre-commit)
    else:
        print("No files needed cleaning.")
        sys.exit(0)

if __name__ == "__main__":
    main()
