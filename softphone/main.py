#!/usr/bin/env python3
"""My Line Telecom Softphone — SIP softphone for Windows.

Usage:
    python main.py
"""

import sys
import os
import logging

# Ensure the softphone package root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler()],
)

from gui.main_window import MainWindow


def main():
    app = MainWindow()
    app.run()


if __name__ == "__main__":
    main()
