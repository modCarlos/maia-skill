#!/usr/bin/env python3
"""
backtest.py — Evalúa los picks históricos contra precios actuales de yfinance.

Lee todos los reportes en output/history/*.json, calcula retornos reales por
horizonte (30 / 60 / 90 días), segmentado por risk_profile.

Escribe data/backtest_summary.json para que mega_agent.py lo inyecte en el prompt.

Uso:
    python3 tools/backtest.py
    python3 tools/backtest.py --min-days 30   # evaluar picks de hace >= 30 días
    python3 tools/backtest.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

try:
    import yfinance as yf
except ImportError:
    print("ERROR: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).parent.parent
HISTORY_DIR = REPO / "output" / "history"
DATA_DIR = REPO / "data"
OUT_PATH = DATA_DIR / "backtest_summary.json"

# Horizons to evaluate (in days).
HORIZONS = [30, 60, 90]
# Minimum sessions before injecting into prompt (avoid N=1 noise).
MIN_SESSIONS_FOR_PROMPT = 3


# ─── Load history ─────────────────────────────────────────────────────────────

def load_history_reports() -> list[tuple[datetime, dict]]:
    """Returns list of (report_date, report_data) sorted oldest-first."""
    if not HISTORY_DIR.exists():
        return []

    reports = []
    for f in sorted(HISTORY_DIR.glob("*.json")):
        # Filename format: YYYY-MM-DD.json
        try:
            date = datetime.strptime(f.stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        reports.append((date, data))

    return sorted(reports, key=lambda x: x[0])


# ─── Price fetching ────────────────────────────────────────────────────────────

_price_cache: dict[str, float | None] = {}


def fetch_current_price(symbol: str) -> float | None:
    """Fetch current price from yfinance. Returns None if unavailable/delisted."""
    if symbol in _price_cache:
        return _price_cache[symbol]
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        price = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("previousClose")
        )
        if price and isinstance(price, (int, float)) and price > 0:
            _price_cache[symbol] = float(price)
            return float(price)
        # Fallback: try fast_info
        fi = getattr(ticker, "fast_info", None)
        if fi:
            p = getattr(fi, "last_price", None)
            if p and p > 0:
                _price_cache[symbol] = float(p)
                return float(p)
    except Exception:
        pass
    _price_cache[symbol] = None
    return None


# ─── Evaluation logic ─────────────────────────────────────────────────────────

def evaluate_pick(pick: dict, report_date: datetime, today: datetime) -> dict | None:
    """
    Evaluate a single pick. Returns result dict or None if skip.

    - ADD  → hit if current_price > entry_price (return > 0)
    - TRIM → hit if current_price < entry_price (return < 0 = trim was correct)
    - HOLD → excluded from hit rate (no directional signal)
    """
    recommendation = (pick.get("recommendation") or "").upper()
    if recommendation not in ("ADD", "TRIM"):
        return None  # HOLD excluded

    symbol = pick.get("symbol", "")
    entry_price = pick.get("entry_price")
    if not symbol or not entry_price or entry_price <= 0:
        return None

    days_elapsed = (today - report_date).days
    if days_elapsed < 1:
        return None

    current_price = fetch_current_price(symbol)
    if current_price is None:
        return {
            "symbol": symbol,
            "recommendation": recommendation,
            "entry_price": entry_price,
            "current_price": None,
            "return_pct": None,
            "hit": None,
            "days_elapsed": days_elapsed,
            "report_date": report_date.strftime("%Y-%m-%d"),
            "no_data": True,
        }

    return_pct = (current_price - entry_price) / entry_price * 100

    if recommendation == "ADD":
        hit = current_price > entry_price
    else:  # TRIM
        hit = current_price < entry_price

    return {
        "symbol": symbol,
        "recommendation": recommendation,
        "entry_price": entry_price,
        "current_price": current_price,
        "return_pct": round(return_pct, 2),
        "hit": hit,
        "days_elapsed": days_elapsed,
        "report_date": report_date.strftime("%Y-%m-%d"),
        "no_data": False,
    }


# ─── Aggregate stats ──────────────────────────────────────────────────────────

def aggregate(results: list[dict], horizon_days: int) -> dict:
    """Stats for picks that are at least `horizon_days` old."""
    subset = [r for r in results if r["days_elapsed"] >= horizon_days and not r["no_data"] and r["hit"] is not None]
    if not subset:
        return {"picks": 0, "hit_rate": None, "avg_return_pct": None}

    hits = sum(1 for r in subset if r["hit"])
    returns = [r["return_pct"] for r in subset]

    return {
        "picks": len(subset),
        "hit_rate": round(hits / len(subset), 3),
        "avg_return_pct": round(sum(returns) / len(returns), 2),
    }


def top_n(results: list[dict], n: int, best: bool) -> list[dict]:
    """Return top N best or worst picks by return_pct."""
    valid = [r for r in results if r["return_pct"] is not None]
    sorted_picks = sorted(valid, key=lambda r: r["return_pct"], reverse=best)
    return [
        {
            "symbol": r["symbol"],
            "recommendation": r["recommendation"],
            "entry": r["entry_price"],
            "current": r["current_price"],
            "return_pct": r["return_pct"],
            "date": r["report_date"],
        }
        for r in sorted_picks[:n]
    ]


def build_prompt_summary(summary: dict) -> str:
    """
    Compact block (< 600 chars) injected into the mega_agent prompt.
    Only built when sessions_evaluated >= MIN_SESSIONS_FOR_PROMPT.
    """
    sessions = summary.get("sessions_evaluated", 0)
    if sessions < MIN_SESSIONS_FOR_PROMPT:
        return ""

    lines = [f"=== HISTORICAL_ACCURACY ({sessions} sessions) ==="]

    for horizon, label in [(30, "30d"), (60, "60d"), (90, "90d")]:
        stats = summary.get("by_horizon", {}).get(str(horizon), {})
        picks = stats.get("picks", 0)
        if picks == 0:
            continue
        hr = stats.get("hit_rate")
        avg_r = stats.get("avg_return_pct")
        hr_str = f"{hr*100:.0f}%" if hr is not None else "—"
        avg_str = f"{avg_r:+.1f}%" if avg_r is not None else "—"
        lines.append(f"  {label}: {picks} picks  hit_rate={hr_str}  avg_return={avg_str}")

    best = summary.get("top_wins", [])[:3]
    worst = summary.get("top_losses", [])[:3]
    if best:
        b_str = "  ".join(f"{r['symbol']}({r['return_pct']:+.1f}%)" for r in best)
        lines.append(f"  ✅ top wins: {b_str}")
    if worst:
        w_str = "  ".join(f"{r['symbol']}({r['return_pct']:+.1f}%)" for r in worst)
        lines.append(f"  ❌ worst:    {w_str}")

    # Per risk_profile
    by_profile = summary.get("by_risk_profile", {})
    for profile, stats in by_profile.items():
        hr = stats.get("hit_rate")
        if hr is not None:
            lines.append(f"  {profile}: hit_rate={hr*100:.0f}% ({stats['picks']} picks)")

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest historical picks")
    parser.add_argument("--min-days", type=int, default=1, help="Minimum days since pick (default: 1)")
    parser.add_argument("--verbose", action="store_true", help="Print per-pick results")
    args = parser.parse_args()

    today = datetime.now(timezone.utc)
    reports = load_history_reports()

    if not reports:
        print("[backtest] No history files found — skipping", file=sys.stderr)
        summary = {
            "generated_at": today.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessions_evaluated": 0,
            "picks_evaluated": 0,
            "no_data_count": 0,
            "by_horizon": {},
            "by_risk_profile": {},
            "top_wins": [],
            "top_losses": [],
            "prompt_block": "",
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
        return 0

    print(f"[backtest] evaluating {len(reports)} session(s)...")

    all_results: list[dict] = []
    by_profile: dict[str, list[dict]] = {}
    sessions_with_picks = 0

    for report_date, data in reports:
        picks = data.get("risk_adjusted_picks") or []
        risk_profile = (data.get("risk_profile") or "unknown").lower()
        session_results = []

        for pick in picks:
            # Polite pacing to avoid yfinance rate limits
            time.sleep(0.1)
            result = evaluate_pick(pick, report_date, today)
            if result is None:
                continue
            session_results.append(result)
            by_profile.setdefault(risk_profile, []).append(result)

            if args.verbose:
                status = "✅" if result["hit"] else ("❌" if result["hit"] is False else "—")
                no_data_flag = " [no_data]" if result["no_data"] else ""
                ret_str = f"{result['return_pct']:+.1f}%" if result["return_pct"] is not None else "?"
                print(
                    f"  {status} {result['symbol']:<6} {result['recommendation']:<4} "
                    f"entry=${result['entry_price']} current=${result['current_price']} "
                    f"ret={ret_str} ({result['days_elapsed']}d){no_data_flag}"
                )

        if session_results:
            sessions_with_picks += 1
        all_results.extend(session_results)

    # Filter by min-days
    filtered = [r for r in all_results if r["days_elapsed"] >= args.min_days]

    no_data_count = sum(1 for r in all_results if r["no_data"])

    # Horizons
    by_horizon = {
        str(h): aggregate(filtered, h) for h in HORIZONS
    }

    # By risk profile (all days)
    profile_stats = {}
    for profile, results in by_profile.items():
        valid = [r for r in results if not r["no_data"] and r["hit"] is not None]
        if not valid:
            continue
        hits = sum(1 for r in valid if r["hit"])
        returns = [r["return_pct"] for r in valid]
        profile_stats[profile] = {
            "picks": len(valid),
            "hit_rate": round(hits / len(valid), 3),
            "avg_return_pct": round(sum(returns) / len(returns), 2),
        }

    wins = top_n(all_results, 5, best=True)
    losses = top_n(all_results, 5, best=False)

    summary = {
        "generated_at": today.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions_evaluated": sessions_with_picks,
        "picks_evaluated": len([r for r in all_results if not r["no_data"]]),
        "no_data_count": no_data_count,
        "by_horizon": by_horizon,
        "by_risk_profile": profile_stats,
        "top_wins": wins,
        "top_losses": losses,
        "prompt_block": "",  # filled below
    }

    summary["prompt_block"] = build_prompt_summary(summary)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[backtest] sessions={sessions_with_picks}  picks={summary['picks_evaluated']}  no_data={no_data_count}")
    for h in HORIZONS:
        stats = by_horizon[str(h)]
        if stats["picks"] > 0:
            hr = f"{stats['hit_rate']*100:.0f}%" if stats["hit_rate"] is not None else "—"
            avg = f"{stats['avg_return_pct']:+.1f}%" if stats["avg_return_pct"] is not None else "—"
            print(f"  {h}d: {stats['picks']} picks  hit={hr}  avg_return={avg}")
    print(f"[backtest] → {OUT_PATH}")

    if sessions_with_picks < MIN_SESSIONS_FOR_PROMPT:
        print(f"[backtest] {sessions_with_picks}/{MIN_SESSIONS_FOR_PROMPT} sessions — prompt injection disabled (need more history)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
