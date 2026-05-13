#!/usr/bin/env python3
"""My Line Telecom Softphone — SIP softphone for Windows.

Usage:
    python main.py
"""

import sys
import os
import logging
from logging.handlers import RotatingFileHandler

# Ensure the softphone package root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Log to %APPDATA%\MyLineSoftphone\softphone.log (survives windowless exe)
_log_dir = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "MyLineSoftphone")
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(_log_dir, "softphone.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),                                    # console (dev)
        RotatingFileHandler(_log_file, maxBytes=1_000_000,            # file (always)
                            backupCount=2, encoding="utf-8"),
    ],
)

from gui.main_window import MainWindow


def main():
    app = MainWindow()
    app.run()


if __name__ == "__main__":
    main()
