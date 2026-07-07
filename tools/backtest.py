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
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

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

# date_str → (open, close) per symbol; populated by load_all_ticker_history()
_ticker_hist: dict[str, dict[str, tuple[float, float]]] = {}

# Max % deviation between LLM entry target and real market open before a pick
# is considered "never executed". Override via env: MAIA_ENTRY_TOLERANCE_PCT=3
# Lowered from 3.0 → 2.0 to match the prompt constraint (entry_price within 2% of current_price).
ENTRY_TOLERANCE_PCT = float(os.getenv("MAIA_ENTRY_TOLERANCE_PCT", "2.0"))

# ─── Vocabulary normalization ──────────────────────────────────────────────────

_REC_ALIASES: dict[str, str] = {
    "ADD": "ADD",
    "BUY": "ADD",
    "TRIM": "TRIM",
    "SELL": "TRIM",
    "REDUCE": "TRIM",
    "HOLD": "HOLD",
    "WATCH": "HOLD",
}


def normalize_rec(raw: str) -> str:
    """Map any recommendation vocabulary to ADD / TRIM / HOLD."""
    return _REC_ALIASES.get(raw.upper().strip(), "HOLD")


# ─── SPY benchmark ─────────────────────────────────────────────────────────────

_spy_closes: dict[str, float] = {}   # "YYYY-MM-DD" → adjusted close


def load_spy_history(start_date: datetime, today: datetime) -> None:
    """Fetch SPY daily closes once from yfinance and cache in _spy_closes."""
    global _spy_closes
    try:
        ticker = yf.Ticker("SPY")
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        hist = ticker.history(start=start_str, end=end_str, auto_adjust=True)
        if hist.empty:
            return
        for ts, row in hist.iterrows():
            date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
            _spy_closes[date_str] = float(row["Close"])
    except Exception as exc:
        print(f"[backtest] SPY history unavailable: {exc}", file=sys.stderr)


def spy_return_for(pick_date: datetime, today: datetime) -> Optional[float]:
    """Return SPY % gain from pick_date to today. None if data missing."""
    if not _spy_closes:
        return None
    date_str = pick_date.strftime("%Y-%m-%d")
    spy_entry = _spy_closes.get(date_str)
    if spy_entry is None:
        # Try the nearest earlier trading day (up to 5 calendar days back)
        for offset in range(1, 6):
            alt = (pick_date - timedelta(days=offset)).strftime("%Y-%m-%d")
            spy_entry = _spy_closes.get(alt)
            if spy_entry:
                break
    if spy_entry is None:
        return None
    # Current SPY = last available close
    current_spy = list(_spy_closes.values())[-1]
    return round((current_spy - spy_entry) / spy_entry * 100, 2)


def load_all_ticker_history(symbols: list[str], start_date: datetime, today: datetime) -> None:
    """
    Batch-download OHLCV for every symbol in one yf.download() call.
    Caches results in _ticker_hist[symbol][date_str] = (open, close).

    Downloads from (start_date - 5 days) to give SPY-adjacent lookback buffer.
    """
    global _ticker_hist
    if not symbols:
        return
    start_str = (start_date - timedelta(days=5)).strftime("%Y-%m-%d")
    end_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        df = yf.download(
            tickers=symbols,
            start=start_str,
            end=end_str,
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        print(f"[backtest] batch ticker download failed: {exc}", file=sys.stderr)
        return
    if df.empty:
        return

    # yf.download returns MultiIndex columns for >1 ticker: (field, symbol)
    # For a single ticker, columns are flat field names.
    open_col = df["Open"]
    close_col = df["Close"]
    is_multi = hasattr(open_col, "columns")  # DataFrame → multi-ticker

    loaded = 0
    for sym in symbols:
        try:
            opens = open_col[sym] if is_multi else open_col
            closes = close_col[sym] if is_multi else close_col
            hist: dict[str, tuple[float, float]] = {}
            for ts in opens.index:
                d = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                try:
                    o = float(opens[ts])
                    c = float(closes[ts])
                    if not (math.isnan(o) or math.isnan(c)):
                        hist[d] = (o, c)
                except (TypeError, ValueError):
                    pass
            if hist:
                _ticker_hist[sym] = hist
                loaded += 1
        except Exception as exc:
            print(f"[backtest] history parse error {sym}: {exc}", file=sys.stderr)

    print(f"[backtest] ticker history loaded: {loaded}/{len(symbols)} symbols", file=sys.stderr)


def true_price_after_pick(symbol: str, pick_date: datetime) -> float | None:
    """
    Return the OPEN price of the first trading day AFTER pick_date.
    Reports run ~4 AM UTC before market open, so next-day open is the
    realistic execution price. Falls back to same-day close if needed.
    """
    hist = _ticker_hist.get(symbol)
    if not hist:
        return None
    pick_str = pick_date.strftime("%Y-%m-%d")
    for d in sorted(hist.keys()):
        if d > pick_str:
            return hist[d][0]  # open price
    # Fallback: same-day close
    entry = hist.get(pick_str)
    return entry[1] if entry else None


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

    - ADD/BUY  → hit if current_price > entry_price
    - TRIM/SELL → hit if current_price < entry_price
    - HOLD     → excluded from hit rate (no directional signal)

    direction_score: return_pct for ADD, -return_pct for TRIM.
    Positive = model was correct. Used for top_wins / top_losses ranking.
    """
    raw_rec = pick.get("recommendation") or ""
    recommendation = normalize_rec(raw_rec)
    if recommendation == "HOLD":
        return None  # no directional signal

    symbol = pick.get("symbol", "")
    llm_entry_price = pick.get("entry_price")
    if not symbol or not llm_entry_price or llm_entry_price <= 0:
        return None

    days_elapsed = (today - report_date).days
    if days_elapsed < 1:
        return None

    spy_ret = spy_return_for(report_date, today)

    # ── Real entry price: next trading day open after the report date ──────────
    # The LLM's entry_price is a *suggested target* (e.g. "buy if it pulls back
    # to $380"), not the actual market price. We use the real next-day open so
    # all backtest returns are grounded in actual executions.
    true_entry = true_price_after_pick(symbol, report_date)
    if true_entry is None:
        # No historical data yet; fall back to LLM target (can't distinguish)
        true_entry = llm_entry_price
        entry_deviation_pct = None
        executed = True
    else:
        entry_deviation_pct = round((true_entry - llm_entry_price) / llm_entry_price * 100, 2)
        executed = abs(entry_deviation_pct) <= ENTRY_TOLERANCE_PCT

    current_price = fetch_current_price(symbol)
    if current_price is None:
        return {
            "symbol": symbol,
            "recommendation": recommendation,
            "llm_entry_price": llm_entry_price,
            "true_entry_price": true_entry,
            "entry_deviation_pct": entry_deviation_pct,
            "executed": executed,
            "current_price": None,
            "return_pct": None,
            "direction_score": None,
            "spy_return_pct": spy_ret,
            "alpha": None,
            "hit": None,
            "days_elapsed": days_elapsed,
            "report_date": report_date.strftime("%Y-%m-%d"),
            "no_data": True,
        }

    # Returns computed from true_entry_price, not the LLM's hypothetical target
    return_pct = round((current_price - true_entry) / true_entry * 100, 2)

    if recommendation == "ADD":
        hit = current_price > true_entry
        direction_score = return_pct
    else:  # TRIM
        hit = current_price < true_entry
        direction_score = -return_pct  # positive = stock fell = TRIM was correct

    alpha = round(direction_score - spy_ret, 2) if spy_ret is not None else None

    return {
        "symbol": symbol,
        "recommendation": recommendation,
        "llm_entry_price": llm_entry_price,
        "true_entry_price": true_entry,
        "entry_deviation_pct": entry_deviation_pct,
        "executed": executed,
        "current_price": current_price,
        "return_pct": return_pct,
        "direction_score": round(direction_score, 2),
        "spy_return_pct": spy_ret,
        "alpha": alpha,
        "hit": hit,
        "days_elapsed": days_elapsed,
        "report_date": report_date.strftime("%Y-%m-%d"),
        "no_data": False,
    }


# ─── Aggregate stats ──────────────────────────────────────────────────────────

def aggregate(results: list[dict], horizon_days: int) -> dict:
    """
    Stats for picks that are at least `horizon_days` old.
    Only picks where executed=True are counted in hit_rate / alpha.
    Picks that were never executed (LLM target too far from real market)
    are tallied separately as missed_entries.
    """
    aged = [r for r in results if r["days_elapsed"] >= horizon_days and not r["no_data"]]
    missed = sum(1 for r in aged if not r.get("executed", True))
    subset = [r for r in aged if r.get("executed", True) and r["hit"] is not None]
    if not subset:
        return {
            "picks": 0,
            "missed_entries": missed,
            "hit_rate": None,
            "avg_direction_score": None,
            "avg_spy_return_pct": None,
            "avg_alpha": None,
        }

    hits = sum(1 for r in subset if r["hit"])
    scores = [r["direction_score"] for r in subset if r["direction_score"] is not None]
    spy_rets = [r["spy_return_pct"] for r in subset if r["spy_return_pct"] is not None]
    alphas = [r["alpha"] for r in subset if r["alpha"] is not None]

    return {
        "picks": len(subset),
        "missed_entries": missed,
        "hit_rate": round(hits / len(subset), 3),
        "avg_direction_score": round(sum(scores) / len(scores), 2) if scores else None,
        "avg_spy_return_pct": round(sum(spy_rets) / len(spy_rets), 2) if spy_rets else None,
        "avg_alpha": round(sum(alphas) / len(alphas), 2) if alphas else None,
    }


def top_n(results: list[dict], n: int, best: bool) -> list[dict]:
    """Return top N best or worst picks by direction_score.

    direction_score > 0 = model was correct (ADD↑ or TRIM↓).
    direction_score < 0 = model was wrong.
    Sorting by direction_score avoids the bug where a TRIM with +62% raw
    return (stock went UP = wrong call) appeared as a 'win'.
    """
    # Only rank executed picks; unexecuted are fictional
    valid = [r for r in results if r.get("direction_score") is not None and r.get("executed", True)]
    sorted_picks = sorted(valid, key=lambda r: r["direction_score"], reverse=best)
    return [
        {
            "symbol": r["symbol"],
            "recommendation": r["recommendation"],
            "llm_entry": r.get("llm_entry_price"),
            "true_entry": r.get("true_entry_price"),
            "entry_deviation_pct": r.get("entry_deviation_pct"),
            "current": r["current_price"],
            "return_pct": r["return_pct"],
            "direction_score": r["direction_score"],
            "spy_return_pct": r.get("spy_return_pct"),
            "alpha": r.get("alpha"),
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
        score = stats.get("avg_direction_score")
        alpha = stats.get("avg_alpha")
        hr_str = f"{hr*100:.0f}%" if hr is not None else "—"
        score_str = f"{score:+.1f}%" if score is not None else "—"
        alpha_str = f"{alpha:+.1f}%" if alpha is not None else "—"
        lines.append(f"  {label}: {picks} picks  hit_rate={hr_str}  avg_score={score_str}  alpha_vs_SPY={alpha_str}")

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
        alpha = stats.get("avg_alpha")
        if hr is not None:
            alpha_str = f"  alpha_vs_SPY={alpha:+.1f}%" if alpha is not None else ""
            lines.append(f"  {profile}: hit_rate={hr*100:.0f}% ({stats['picks']} picks){alpha_str}")

    # Per-symbol repeat losers (compact — only symbols with >=2 consecutive losses)
    per_symbol = summary.get("per_symbol", {})
    repeat_losers = [
        (sym, s) for sym, s in per_symbol.items()
        if s.get("consecutive_losses", 0) >= 2
    ]
    if repeat_losers:
        loser_parts = []
        for sym, s in sorted(repeat_losers, key=lambda x: -x[1]["consecutive_losses"])[:5]:
            loser_parts.append(f"{sym}({s['consecutive_losses']}L)")
        lines.append(f"  ⚠ repeat losers: {' '.join(loser_parts)}")

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

    oldest_date = reports[0][0]

    # Load SPY history once for the full date range
    load_spy_history(oldest_date, today)
    if _spy_closes:
        print(f"[backtest] SPY history loaded ({len(_spy_closes)} trading days)", file=sys.stderr)
    else:
        print("[backtest] SPY history unavailable — alpha will be null", file=sys.stderr)

    # Batch-download real OHLCV for every symbol so true_entry_price is available.
    # One API call instead of N per-ticker calls; much faster and rate-limit friendly.
    unique_symbols = list({
        pick.get("symbol", "")
        for _, data in reports
        for pick in (data.get("risk_adjusted_picks") or [])
        if pick.get("symbol")
    })
    if unique_symbols:
        load_all_ticker_history(unique_symbols, oldest_date, today)

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
                exec_flag = "" if result.get("executed", True) else " [NOT_EXECUTED]"
                ret_str = f"{result['return_pct']:+.1f}%" if result["return_pct"] is not None else "?"
                dev_str = (
                    f" dev={result['entry_deviation_pct']:+.1f}%"
                    if result.get("entry_deviation_pct") is not None else ""
                )
                print(
                    f"  {status} {result['symbol']:<6} {result['recommendation']:<4} "
                    f"llm=${result['llm_entry_price']} true=${result['true_entry_price']} "
                    f"current=${result['current_price']} ret={ret_str}{dev_str} "
                    f"({result['days_elapsed']}d){no_data_flag}{exec_flag}"
                )

        if session_results:
            sessions_with_picks += 1
        all_results.extend(session_results)

    # Filter by min-days — used only for print summary, NOT for horizon buckets.
    # Horizons use all_results so that --min-days doesn't silently empty shorter buckets.
    # Example: --min-days 60 would zero out the 30d bucket if we passed filtered to aggregate().
    filtered = [r for r in all_results if r["days_elapsed"] >= args.min_days]

    no_data_count = sum(1 for r in all_results if r["no_data"])
    missed_entries_count = sum(1 for r in all_results if not r.get("executed", True))

    # Horizons — always computed on full all_results, independent of --min-days
    by_horizon = {
        str(h): aggregate(all_results, h) for h in HORIZONS
    }

    # By risk profile (all days, executed picks only)
    profile_stats = {}
    for profile, results in by_profile.items():
        executed = [r for r in results if not r["no_data"] and r.get("executed", True) and r["hit"] is not None]
        missed_p = sum(1 for r in results if not r.get("executed", True))
        if not executed:
            continue
        hits = sum(1 for r in executed if r["hit"])
        scores = [r["direction_score"] for r in executed if r.get("direction_score") is not None]
        alphas = [r["alpha"] for r in executed if r.get("alpha") is not None]
        profile_stats[profile] = {
            "picks": len(executed),
            "missed_entries": missed_p,
            "hit_rate": round(hits / len(executed), 3),
            "avg_direction_score": round(sum(scores) / len(scores), 2) if scores else None,
            "avg_alpha": round(sum(alphas) / len(alphas), 2) if alphas else None,
        }

    wins = top_n(all_results, 5, best=True)
    losses = top_n(all_results, 5, best=False)

    # ── Per-symbol summary: hit rate + consecutive losses ─────────────────────
    # Used by mega_agent.py to penalize repeat losers and avoid re-recommending
    # symbols that have failed multiple sessions in a row (e.g. CRM ADD ×3 while falling).
    by_symbol: dict[str, list[dict]] = {}
    for r in all_results:
        if r.get("no_data") or not r.get("executed", True) or r.get("hit") is None:
            continue
        sym = r["symbol"]
        by_symbol.setdefault(sym, []).append(r)

    per_symbol: dict[str, dict] = {}
    for sym, results in by_symbol.items():
        # Sort chronologically to detect consecutive losses at the tail
        sorted_r = sorted(results, key=lambda x: x["report_date"])
        hits = sum(1 for r in sorted_r if r["hit"])
        scores = [r["direction_score"] for r in sorted_r if r.get("direction_score") is not None]
        alphas = [r["alpha"] for r in sorted_r if r.get("alpha") is not None]
        # Count consecutive losses from the most recent pick backwards
        consecutive_losses = 0
        for r in reversed(sorted_r):
            if not r["hit"]:
                consecutive_losses += 1
            else:
                break
        per_symbol[sym] = {
            "sessions": len(sorted_r),
            "hit_rate": round(hits / len(sorted_r), 3),
            "avg_direction_score": round(sum(scores) / len(scores), 2) if scores else None,
            "avg_alpha": round(sum(alphas) / len(alphas), 2) if alphas else None,
            "consecutive_losses": consecutive_losses,
            "last_recommendation": sorted_r[-1]["recommendation"],
            "last_date": sorted_r[-1]["report_date"],
        }

    summary = {
        "generated_at": today.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sessions_evaluated": sessions_with_picks,
        "picks_evaluated": len([r for r in all_results if not r["no_data"] and r.get("executed", True)]),
        "no_data_count": no_data_count,
        "missed_entries_count": missed_entries_count,
        "entry_tolerance_pct": ENTRY_TOLERANCE_PCT,
        "by_horizon": by_horizon,
        "by_risk_profile": profile_stats,
        "per_symbol": per_symbol,
        "top_wins": wins,
        "top_losses": losses,
        "prompt_block": "",  # filled below
    }

    summary["prompt_block"] = build_prompt_summary(summary)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(
        f"[backtest] sessions={sessions_with_picks}  "
        f"picks={summary['picks_evaluated']}  "
        f"no_data={no_data_count}  "
        f"missed_entries={missed_entries_count} (tol={ENTRY_TOLERANCE_PCT}%)"
    )
    for h in HORIZONS:
        stats = by_horizon[str(h)]
        if stats["picks"] > 0:
            hr = f"{stats['hit_rate']*100:.0f}%" if stats["hit_rate"] is not None else "—"
            score = f"{stats['avg_direction_score']:+.1f}%" if stats.get("avg_direction_score") is not None else "—"
            alpha = f"{stats['avg_alpha']:+.1f}%" if stats.get("avg_alpha") is not None else "—"
            print(f"  {h}d: {stats['picks']} picks  hit={hr}  avg_score={score}  alpha_vs_SPY={alpha}")
    print(f"[backtest] → {OUT_PATH}")

    if sessions_with_picks < MIN_SESSIONS_FOR_PROMPT:
        print(f"[backtest] {sessions_with_picks}/{MIN_SESSIONS_FOR_PROMPT} sessions — prompt injection disabled (need more history)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
