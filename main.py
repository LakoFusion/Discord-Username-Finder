#!/usr/bin/env python3
"""Discord Username Finder – Entry point (thin launcher).

Chạy:  python main.py [--no-wizard] [-d]
Mọi logic nằm trong src/ (checker, wizard, engine, proxy, ui, config).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Đảm bảo project root luôn trong sys.path (chạy từ bất kỳ cwd nào cũng được)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.checker import main

if __name__ == "__main__":
    main()
