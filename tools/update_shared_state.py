#!/usr/bin/env python3
"""
update_shared_state.py — Genera data/shared_state.json con los picks del screener.

Debe ejecutarse DESPUÉS de write_report.py en run_local.sh.
El portfolio_agent lo leerá en el siguiente run de run_portfolio.sh.

Uso:
    python3 tools/update_shared_state.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

REPO = Path(__file__).parent.parent
DATA_DIR = REPO / "data"
REPORT_PATH = REPO / "dashboard" / "public" / "data" / "report.json"
PORTFOLIO_PATH = DATA_DIR / "portfolio.json"
OUT_PATH = DATA_DIR / "shared_state.json"


def load_json(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def empty_state(reason: str) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "screener": {"date": None, "risk_profile": None, "add": [], "hold": [], "trim": []},
        "note": reason,
    }


def main() -> int:
    report = load_json(REPORT_PATH)
    if not report:
        state = empty_state("no report.json found — run run_local.sh first")
        OUT_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[shared_state] no report found — wrote empty state → {OUT_PATH}", file=sys.stderr)
        return 0

    picks = report.get("risk_adjusted_picks") or []
    if not picks:
        state = empty_state("report.json has no risk_adjusted_picks")
        OUT_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[shared_state] no picks in report — wrote empty state", file=sys.stderr)
        return 0

    # Agrupar picks por acción
    add_picks  = [p["symbol"] for p in picks if (p.get("recommendation") or "").upper() == "ADD"]
    hold_picks = [p["symbol"] for p in picks if (p.get("recommendation") or "").upper() == "HOLD"]
    trim_picks = [p["symbol"] for p in picks if (p.get("recommendation") or "").upper() == "TRIM"]

    # Fecha del reporte
    report_date = (report.get("generated_at") or "")[:10]
    risk_profile = report.get("risk_profile", "unknown")

    state = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "screener": {
            "date": report_date,
            "risk_profile": risk_profile,
            "add": add_picks,
            "hold": hold_picks,
            "trim": trim_picks,
        },
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"[shared_state] {report_date} | {risk_profile} | "
        f"ADD={len(add_picks)} HOLD={len(hold_picks)} TRIM={len(trim_picks)} → {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
