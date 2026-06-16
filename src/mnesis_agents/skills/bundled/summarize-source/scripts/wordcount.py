#!/usr/bin/env python3
"""Trivial bundled script (EXAMPLE): print the word count of a file argument
(or stdin). Demonstrates guarded skill-script execution — not a real tool."""
import sys

data = open(sys.argv[1], encoding="utf-8").read() if len(sys.argv) > 1 else sys.stdin.read()
print(len(data.split()))
