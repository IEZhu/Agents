"""Check if Python version meets the minimum requirement (>= 3.10).

Usage: python check_version.py
Prints major.minor version if supported, exits with 1 otherwise.
"""
import sys

v = sys.version_info
if v[:2] < (3, 10):
    sys.exit(1)
print(f"{v.major}.{v.minor}")
