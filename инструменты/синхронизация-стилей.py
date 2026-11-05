#!/usr/bin/env python3
"""Copy shared demo styles into the Frappe app without copying local accounting code."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ASSETS = {
    "dist/assets/app.css": "erp_basic/public/erp-base.css",
    "dist/assets/themes.css": "erp_basic/public/themes.css",
    "dist/theme.js": "erp_basic/public/theme.js",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Проверить синхронизацию без записи")
    args = parser.parse_args()
    mismatches = []
    for source, target in ASSETS.items():
        original = (ROOT / source).read_bytes()
        destination = ROOT / target
        if args.check:
            if not destination.exists() or destination.read_bytes() != original:
                mismatches.append(target)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(original)
    if mismatches:
        print("Обновите общие стили: python3 инструменты/синхронизация-стилей.py", file=sys.stderr)
        print("\n".join(mismatches), file=sys.stderr)
        return 1
    print("Общие стили и темы ERP Basic синхронизированы.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
