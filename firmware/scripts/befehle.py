"""Deprecated: renamed to trace_commands.py (old name kept for the PSX monitor; default output is format 1)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from trace_commands import *  # noqa: E402,F401,F403
from trace_commands import main as _main  # noqa: E402


def main(argv=None):
    return _main(argv, default_format=1)


if __name__ == "__main__":
    sys.exit(main())
