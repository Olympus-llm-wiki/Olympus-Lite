#!/usr/bin/env python3
"""Run from a checkout without installing packages."""
import sys

if sys.version_info < (3, 11):
    raise SystemExit("Olympus-Lite requires Python 3.11 or newer.")

from olympus_lite.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
