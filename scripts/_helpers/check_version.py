"""Check if Python version is in supported range (3.10 - 3.12).

Usage: python check_version.py
Prints major.minor version if supported, exits with 1 otherwise.
"""
import sys

v = sys.version_info
if not ((3, 10) <= v[:2] < (3, 13)):
    sys.exit(1)
print(f"{v.major}.{v.minor}")
