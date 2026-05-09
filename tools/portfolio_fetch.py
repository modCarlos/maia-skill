#!/usr/bin/env python3
"""
portfolio_fetch.py — Fetches fresh market data + news for portfolio positions only.

Reads data/portfolio.json, fetches RSI/price/fundamentals via yfinance,
and writes data/portfolio_market.json for the MegaAgent prompt.

Usage:
    python3 tools/portfolio_fetch.py

Output:
    data/portfolio_market.json
"""

import json
import warnings
import logging
import sys
import os
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

import yfinance as yf
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data")
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
OUTPUT_FILE = os.path.join(DATA_DIR, "portfolio_market.json")

# ---------------------------------------------------------------------------
# RSI helper
# ---------------------------------------------------------------------------
def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return round(float(rsi.iloc[-1]), 1) if not rsi.empty else None

# ---------------------------------------------------------------------------
# News helpers (same keyword approach as news_fetch.py)
# ---------------------------------------------------------------------------
_POSITIVE_KW = [
    "beat", "beats", "exceeds", "record", "growth", "breakthrough", "surge",
    "upgrade", "upgraded", "raises", "raised", "buyback", "dividend",
    "expansion", "partnership", "approval", "approved", "outperform",
    "strong", "rally", "profit", "milestone", "award", "innovation",
]
_NEGATIVE_KW = [
    "miss", "misses", "missed", "downgrade", "downgraded", "loss", "losses",
    "lawsuit", "investigation", "recall", "warning", "bankruptcy", "crisis",
    "decline", "regulation", "fine", "violation", "scandal", "cut", "cuts",
    "lowers", "lowered", "weak", "disappoints", "disappointed", "slump",
]

def score_sentiment(text: str) -> str:
    t = text.lower()
    pos = sum(1 for w in _POSITIVE_KW if w in t)
    neg = sum(1 for w in _NEGATIVE_KW if w in t)
    if pos > neg:
        return "bullish"
    elif neg > pos:
        return "bearish"
    return "neutral"

def fetch_news(ticker_obj) -> list[str]:
    try:
        news = ticker_obj.news or []
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        headlines = []
        for item in news[:10]:
            pub = item.get("content", {}).get("pubDate") or item.get("providerPublishTime", 0)
            if isinstance(pub, (int, float)):
                pub_dt = datetime.fromtimestamp(pub, tz=timezone.utc)
                if pub_dt < cutoff:
                    continue
            title = item.get("content", {}).get("title") or item.get("title", "")
            if title:
                headlines.append(title)
        return headlines[:5]
    except Exception:
        return []

# ---------------------------------------------------------------------------
# Per-ticker fetch
# ---------------------------------------------------------------------------
def fetch_ticker(entry: dict) -> dict:
    symbol = entry["symbol"]
    buy_price = entry["buyPrice"]
    quantity = entry["quantity"]

    result = {
        "symbol": symbol,
        "name": entry.get("name", symbol),
        "sector": entry.get("sector", ""),
        "buy_price": buy_price,
        "quantity": quantity,
        "buy_date": entry.get("buyDate", ""),
        "cost_basis": round(buy_price * quantity, 2),
        "current_price": None,
        "change_pct_1d": None,
        "change_pct_7d": None,
        "change_pct_30d": None,
        "pnl_amount": None,
        "pnl_pct": None,
        "rsi_14": None,
        "week_52_high": None,
        "week_52_low": None,
        "pct_from_52w_high": None,
        "pct_from_52w_low": None,
        "market_cap": None,
        "pe_ratio": None,
        "forward_pe": None,
        "peg_ratio": None,
        "analyst_target": None,
        "analyst_upside": None,
        "recommendation_mean": None,
        "trend": "unknown",
        "news_headlines": [],
        "news_sentiment": "neutral",
        "error": None,
    }

    try:
        tkr = yf.Ticker(symbol)
        hist_1y = tkr.history(period="1y", interval="1d", auto_adjust=True)

        if hist_1y.empty:
            result["error"] = "no_data"
            return result

        closes = hist_1y["Close"]
        current = float(closes.iloc[-1])
        result["current_price"] = round(current, 4)

        # P&L vs buy price
        result["pnl_amount"] = round((current - buy_price) * quantity, 2)
        result["pnl_pct"] = round((current - buy_price) / buy_price * 100, 2)

        # Changes
        if len(closes) >= 2:
            result["change_pct_1d"] = round((closes.iloc[-1] / closes.iloc[-2] - 1) * 100, 2)
        if len(closes) >= 7:
            result["change_pct_7d"] = round((closes.iloc[-1] / closes.iloc[-7] - 1) * 100, 2)
        if len(closes) >= 30:
            result["change_pct_30d"] = round((closes.iloc[-1] / closes.iloc[-30] - 1) * 100, 2)

        # 52w range
        high52 = float(closes[-252:].max()) if len(closes) >= 252 else float(closes.max())
        low52 = float(closes[-252:].min()) if len(closes) >= 252 else float(closes.min())
        result["week_52_high"] = round(high52, 4)
        result["week_52_low"] = round(low52, 4)
        result["pct_from_52w_high"] = round((current - high52) / high52 * 100, 2)
        result["pct_from_52w_low"] = round((current - low52) / low52 * 100, 2)

        # RSI
        result["rsi_14"] = calc_rsi(closes)

        # Trend (SMA20 vs SMA50)
        if len(closes) >= 50:
            sma20 = float(closes.rolling(20).mean().iloc[-1])
            sma50 = float(closes.rolling(50).mean().iloc[-1])
            result["trend"] = "uptrend" if sma20 > sma50 else "downtrend"

        # Fundamentals
        info = tkr.info or {}
        result["market_cap"] = info.get("marketCap")
        result["pe_ratio"] = info.get("trailingPE")
        result["forward_pe"] = info.get("forwardPE")
        result["peg_ratio"] = info.get("pegRatio")
        result["analyst_target"] = info.get("targetMeanPrice")
        if result["analyst_target"] and current:
            result["analyst_upside"] = round((result["analyst_target"] - current) / current * 100, 2)
        result["recommendation_mean"] = info.get("recommendationMean")  # 1=strong buy, 5=strong sell

        # News
        headlines = fetch_news(tkr)
        result["news_headlines"] = headlines
        all_text = " ".join(headlines)
        result["news_sentiment"] = score_sentiment(all_text) if all_text else "neutral"

    except Exception as e:
        result["error"] = str(e)

    return result

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not os.path.exists(PORTFOLIO_FILE):
        print(f"ERROR: {PORTFOLIO_FILE} not found. No positions to analyze.", file=sys.stderr)
        sys.exit(1)

    portfolio = json.loads(open(PORTFOLIO_FILE, encoding="utf-8").read())
    if not portfolio:
        print("Portfolio is empty.", file=sys.stderr)
        sys.exit(0)

    symbols = [e["symbol"] for e in portfolio]
    print(f"Fetching data for {len(symbols)} positions: {', '.join(symbols)}")

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_ticker, entry): entry["symbol"] for entry in portfolio}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                r = fut.result()
                results.append(r)
                price = r.get("current_price")
                pnl = r.get("pnl_pct")
                rsi = r.get("rsi_14")
                print(f"  {sym}: price=${price} pnl={pnl:+.2f}% rsi={rsi}" if pnl is not None else f"  {sym}: {r.get('error','?')}")
            except Exception as e:
                print(f"  {sym}: ERROR {e}")

    # Sort by original portfolio order
    order = {e["symbol"]: i for i, e in enumerate(portfolio)}
    results.sort(key=lambda r: order.get(r["symbol"], 999))

    # Macro snapshot from market_context if available
    macro = {}
    mc_path = os.path.join(DATA_DIR, "market_context.json")
    if os.path.exists(mc_path):
        try:
            mc = json.loads(open(mc_path, encoding="utf-8").read())
            macro = mc.get("macro", {})
        except Exception:
            pass

    output = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "positions": results,
        "macro": macro,
        "total_cost": round(sum(r["cost_basis"] for r in results), 2),
        "total_current_value": round(sum((r["current_price"] or 0) * r["quantity"] for r in results), 2),
    }
    output["total_pnl"] = round(output["total_current_value"] - output["total_cost"], 2)
    output["total_pnl_pct"] = round(output["total_pnl"] / output["total_cost"] * 100, 2) if output["total_cost"] else 0

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nOK → {OUTPUT_FILE}")
    print(f"Portfolio: cost=${output['total_cost']} | value=${output['total_current_value']} | P&L={output['total_pnl_pct']:+.2f}%")

if __name__ == "__main__":
    main()
