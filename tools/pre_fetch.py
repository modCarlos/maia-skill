#!/usr/bin/env python3
"""
Tododeia Pre-fetch Script (Versión Mejorada)
============================================
Calculates real technical indicators, valuation metrics, earnings dates,
and insider signals BEFORE LLM agents run.

MEJORAS REALIZADAS:
- Manejo mejorado de datos faltantes con cálculos matemáticos alternativos (Fallbacks)
- Cálculo manual de FCF para empresas sin métrica directa y Price to FCF alternativo
- Cálculo manual de PEG Ratio y Payout Ratio ante valores nulos de la API
- Soporte para ETFs (evaluación solo técnica)
- Métricas alternativas para REITs (AFFO) y Financieros (Net Income Margin)
- Fallback para tickers internacionales con datos limitados
- Mejor manejo de errores y timeouts

Usage:
    python3 pre_fetch.py [TICKER1 TICKER2 ...]
    python3 pre_fetch.py              # uses DEFAULT_TICKERS

Output:
    data/market_context.json
"""

import sys
import os
import json
import glob
import warnings
import time
from typing import Optional, Dict, Any, List

# Force UTF-8 output for Windows compatibility
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")
warnings.filterwarnings("ignore")
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import urllib.request
import yfinance as yf
import pandas as pd
import numpy as np

# ─── Watchlists ───────────────────────────────────────────────────────────────
_CORE = [
    "NVDA", "AMD", "TSM", "AMZN", "MSFT", "AAPL",
    "GOOGL", "META", "AVGO", "BAC", "JPM", "PLTR",
    "TSLA", "NFLX", "KMI",
]

# Named watchlists
WATCHLISTS = {
    "default": _CORE,
    "tech": [
        "NVDA", "AMD", "TSM", "INTC", "QCOM", "ARM",
        "MSFT", "AAPL", "GOOGL", "META", "AMZN", "IBM",
        "AVGO", "AMAT", "ASML",
        "PLTR", "SNOW", "CRM", "NOW", "PANW",
        "NFLX", "TSLA", "RIVN", "SONY",
    ],
    "financials": [
        "JPM", "BAC", "GS", "MS", "WFC", "C",
        "V", "MA", "PYPL",
        "BLK", "BX",
        "NU", "SOFI",
    ],
    "consumer": [
        "DIS", "NFLX",
        "HD", "SBUX",
        "BABA", "MELI",
    ],
    "materials": [
        "XOM", "CVX", "COP", "KMI", "OXY",
        "FCX", "NEM",
    ],
    "healthcare": [
        "LLY", "UNH", "JNJ", "ABBV", "MRK",
        "AMGN", "GILD", "REGN", "MRNA", "PFE",
    ],
    "all": sorted(set(
        _CORE +
        ["INTC", "QCOM", "ARM", "AMAT", "ASML", "SNOW", "CRM", "NOW", "PANW", "CRWD", "SMCI", "VRT", "ANET"] +
        ["JPM", "BAC", "GS", "MS", "WFC", "C", "V", "MA", "PYPL", "BLK", "BX"] +
        ["GLD", "SLV", "GDX", "GDXJ", "XOM", "CVX", "COP", "OXY", "FCX", "NEM"] +
        ["LLY", "UNH", "JNJ", "ABBV", "MRK", "AMGN", "GILD", "REGN", "MRNA", "PFE"] +
        ["SONY", "BABA", "TCEHY", "RIVN", "MELI", "NU", "SOFI", "DIS", "HD", "SBUX", "IBM",
         "WMT", "COST", "PG", "KO", "PEP", "CAT", "HON", "UPS", "BA", "T", "VZ",
         "O", "PLD", "AMT", "ORCL", "MMM", "NKE", "CMG", "F"]
    )),
}

DEFAULT_TICKERS = WATCHLISTS["all"]
MACRO_TICKERS = ["^VIX", "^TNX", "^GSPC", "^IRX", "DX-Y.NYB"]
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(SKILL_ROOT, "data", "market_context.json")

# ─── Correlation groups ───────────────────────────────────────────────────────
CORRELATION_GROUPS: Dict[str, List[str]] = {
    "precious_metals":        ["GLD", "SLV", "GDX", "GDXJ", "NEM"],
    "semiconductors":         ["NVDA", "AMD", "INTC", "QCOM", "TSM", "ARM", "AMAT", "ASML", "AVGO", "ANET", "SMCI", "VRT"],
    "big_tech":               ["MSFT", "AAPL", "GOOGL", "META", "AMZN", "IBM", "PLTR", "SONY"],
    "financials":             ["JPM", "BAC", "GS", "MS", "WFC", "C"],
    "payments":               ["V", "MA", "PYPL"],
    "healthcare":             ["JNJ", "ABBV", "MRK", "AMGN", "GILD", "REGN", "LLY", "UNH", "MRNA", "PFE"],
    "staples":                ["WMT", "COST", "PG", "KO", "PEP"],
    "industrials":            ["CAT", "HON", "UPS", "BA", "MMM"],
    "telecom":                ["T", "VZ"],
    "energy":                 ["XOM", "CVX", "COP", "OXY", "KMI"],
    "base_metals":            ["FCX"],
    "saas":                   ["CRM", "NOW", "SNOW", "PANW", "CRWD"],
    "enterprise_software":    ["ORCL"],
    "asset_managers":         ["BLK", "BX"],
    "ev":                     ["TSLA", "RIVN"],
    "streaming":              ["NFLX", "DIS"],
    "ecommerce_global":       ["BABA", "MELI", "TCEHY"],
    "fintech":                ["NU", "SOFI"],
    "consumer_discretionary": ["HD", "SBUX", "CMG", "NKE"],
    "reits":                  ["O", "PLD", "AMT"],
}

_SYMBOL_TO_GROUP: Dict[str, str] = {
    sym: group
    for group, symbols in CORRELATION_GROUPS.items()
    for sym in symbols
}

MAX_PICKS_PER_GROUP = 2

# ─── Funciones de utilidad mejoradas ────────────────────────────────────────

def safe_get(info: Dict, key: str, default=None):
    """Obtiene valor de info de forma segura, manejando valores inválidos."""
    try:
        v = info.get(key)
        if v in (None, "N/A", "NaN", float("inf"), float("-inf"), "Infinity", "-Infinity"):
            return default
        if isinstance(v, (int, float)) and (pd.isna(v) or not np.isfinite(v)):
            return default
        return v
    except (TypeError, ValueError, AttributeError):
        return default

def round_safe(value, decimals=4):
    """Redondea un valor de forma segura, manejando None."""
    if value is None:
        return None
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return None

def safe_percent(value):
    """Convierte un valor a porcentaje de forma segura."""
    if value is None:
        return None
    try:
        return round(float(value) * 100, 2)
    except (TypeError, ValueError):
        return None

def detect_asset_type(info: Dict) -> str:
    """Detecta el tipo de activo basado en información de Yahoo Finance."""
    quote_type = safe_get(info, "quoteType", "")
    if quote_type == "ETF":
        return "etf"
    if quote_type == "MUTUALFUND":
        return "mutual_fund"
    if quote_type == "INDEX":
        return "index"
    # Detectar REITs por nombre o sector
    sector = safe_get(info, "sector", "")
    long_name = safe_get(info, "longName", "")
    if "REIT" in long_name or "reit" in long_name or sector == "Real Estate":
        return "reit"
    # Detectar Financials
    if sector in ["Financial Services", "Financial", "Banks"]:
        return "financial"
    return "stock"

def detect_sector(info: Dict) -> str:
    """Detecta el sector de la empresa."""
    sector = safe_get(info, "sector", "")
    if not sector:
        long_name = safe_get(info, "longName", "")
        if any(x in long_name for x in ["Bank", "Financial", "Investment", "Asset Management"]):
            return "Financial"
        if any(x in long_name for x in ["REIT", "Real Estate"]):
            return "Real Estate"
        if any(x in long_name for x in ["Pharmaceutical", "Healthcare", "Biotech", "Medical"]):
            return "Healthcare"
        if any(x in long_name for x in ["Semiconductor", "Chip", "Processor"]):
            return "Technology"
    return sector

# ─── Technical indicators ──────────────────────────────────────────────────────

def _rsi(closes: pd.Series, window: int = 14) -> float:
    """RSI con suavizado de Wilder."""
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / window, adjust=False).mean()
    loss = loss.replace(0, np.nan)
    rs = gain / loss
    rsi_series = 100 - (100 / (1 + rs))
    if rsi_series.isna().all():
        return 50.0
    return round(float(rsi_series.iloc[-1]), 1)

def _macd_signal(closes: pd.Series, slow=26, fast=12, signal=9) -> str:
    exp_fast = closes.ewm(span=fast, adjust=False).mean()
    exp_slow = closes.ewm(span=slow, adjust=False).mean()
    macd = exp_fast - exp_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    if len(hist) < 2:
        return "neutral"
    macd_val = macd.iloc[-1]
    sig_val = sig.iloc[-1]
    hist_now = hist.iloc[-1]
    hist_prev = hist.iloc[-2]
    if macd_val > sig_val:
        return "bullish" if hist_now >= hist_prev else "bullish_weakening"
    else:
        return "bearish" if hist_now <= hist_prev else "bearish_weakening"

def _trend(closes: pd.Series) -> str:
    price = closes.iloc[-1]
    sma50 = closes.rolling(50).mean().iloc[-1]
    sma200 = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else None
    if sma200 is not None and not pd.isna(sma200):
        if price > sma50 and price > sma200:
            return "uptrend"
        elif price < sma50 and price < sma200:
            return "downtrend"
        else:
            return "mixed"
    return "uptrend" if price > sma50 else "downtrend"

def _support_resistance(closes: pd.Series):
    price = closes.iloc[-1]
    sma50 = closes.rolling(50).mean().iloc[-1]
    sma200_series = closes.rolling(200).mean()
    sma200 = sma200_series.iloc[-1] if len(closes) >= 200 else None
    low_52w = closes.tail(252).min()
    high_52w = closes.tail(252).max()
    smas_below = [s for s in [sma50, sma200] if s is not None and not pd.isna(s) and s < price]
    support = round(max(smas_below) if smas_below else low_52w, 2)
    resistance = round(high_52w, 2)
    return support, resistance

def _atr(high: pd.Series, low: pd.Series, closes: pd.Series, window: int = 14) -> Optional[float]:
    try:
        prev_close = closes.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / window, min_periods=window, adjust=False).mean()
        val = float(atr.iloc[-1])
        return round(val, 2) if not pd.isna(val) else None
    except Exception:
        return None

def _entry_quality(
    rsi: float,
    price: float,
    support: float,
    fcf_margin: Optional[float] = None,
    revenue_growth: Optional[float] = None,
    asset_type: str = "stock",
) -> str:
    pct_above = (price - support) / support * 100 if support > 0 else 0
    
    # Para ETFs, evaluación más simple
    if asset_type in ["etf", "mutual_fund", "index"]:
        if rsi < 35:
            return "excellent — oversold (ETF)"
        elif rsi < 55 and pct_above < 5:
            return "good — near support (ETF)"
        elif rsi > 70:
            return "poor — overbought (ETF)"
        elif pct_above > 15:
            return "poor — extended (ETF)"
        return "fair (ETF)"
    
    if rsi < 35:
        has_data = fcf_margin is not None or revenue_growth is not None
        if not has_data:
            return "excellent — oversold (verify)"
        good_fcf = fcf_margin is None or fcf_margin > 0.10
        not_shrinking = revenue_growth is None or revenue_growth > -0.05
        if good_fcf and not_shrinking:
            return "excellent — oversold"
        elif not good_fcf and revenue_growth is not None and revenue_growth < -0.05:
            return "oversold — fundamentals deteriorating"
        else:
            return "oversold — verify fundamentals"
    elif rsi < 55 and pct_above < 5:
        return "good — near support"
    elif rsi > 70:
        return "poor — overbought"
    elif pct_above > 15:
        return "poor — extended"
    else:
        return "fair"

# ─── Earnings ──────────────────────────────────────────────────────────────────

def _earnings(ticker_obj) -> Dict:
    result = {"next_date": None, "days_away": None, "last_surprise_pct": None, "beat_streak": 0}
    try:
        info = ticker_obj.info
        ts = safe_get(info, "earningsTimestamp")
        if ts:
            dt = pd.to_datetime(ts, unit="s")
            result["next_date"] = dt.strftime("%Y-%m-%d")
            result["days_away"] = int((dt - pd.Timestamp.now()).days)
    except Exception:
        pass
    try:
        hist = ticker_obj.get_earnings_history()
        if hist is not None and not hist.empty:
            col = next((c for c in ["surprisePercent", "Surprise(%)"] if c in hist.columns), None)
            if col:
                surprises = hist[col].dropna().head(4).tolist()
                if surprises:
                    result["last_surprise_pct"] = round_safe(surprises[0], 2)
                    result["beat_streak"] = int(sum(1 for s in surprises if s > 0))
    except Exception:
        pass
    return result

def _insider(ticker_obj) -> str:
    try:
        txns = ticker_obj.get_insider_transactions()
        if txns is None or txns.empty:
            return "neutral"
        cutoff = pd.Timestamp.now() - pd.Timedelta(days=90)
        date_col = next((c for c in ["Start Date", "startDate", "Date", "date"] if c in txns.columns), None)
        if date_col:
            txns[date_col] = pd.to_datetime(txns[date_col], errors="coerce")
            recent = txns[txns[date_col] >= cutoff]
        else:
            recent = txns
        buys = sells = 0
        for _, row in recent.iterrows():
            text = str(row.get("Transaction", row.get("transaction", ""))).lower()
            if any(k in text for k in ("purchase", "buy", "acquisition")):
                buys += 1
            elif any(k in text for k in ("sale", "sell", "sold")):
                sells += 1
        if buys > sells * 1.5:
            return "bullish"
        elif sells > buys * 1.5:
            return "bearish"
        return "neutral"
    except Exception:
        return "neutral"

# ─── Cálculo de FCF mejorado ──────────────────────────────────────────────────

def calculate_fcf(info: Dict) -> tuple:
    """
    Calcula Free Cash Flow de forma robusta.
    Retorna (fcf, fcf_margin, fcf_abs, operating_cash_flow, capital_expenditures)
    """
    revenue = safe_get(info, "totalRevenue")
    operating_cash_flow = safe_get(info, "operatingCashFlow")
    capital_expenditures = safe_get(info, "capitalExpenditures")
    free_cashflow = safe_get(info, "freeCashflow")
    
    # Si ya existe FCF, usarlo
    if free_cashflow is not None:
        fcf = free_cashflow
    # Si no, calcular desde Operating Cash Flow - CapEx
    elif operating_cash_flow is not None and capital_expenditures is not None:
        fcf = operating_cash_flow - abs(capital_expenditures)
    else:
        fcf = None
    
    fcf_margin = None
    if fcf is not None and revenue is not None and revenue > 0:
        fcf_margin = round_safe(fcf / revenue, 4)
    
    fcf_abs = fcf
    
    return fcf, fcf_margin, fcf_abs, operating_cash_flow, capital_expenditures

# ─── Per-stock fetch mejorado ─────────────────────────────────────────────────

def fetch_stock(symbol: str) -> Optional[Dict]:
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period="1y", interval="1d")
        if hist.empty:
            return None
        
        closes = hist["Close"]
        price = round_safe(closes.iloc[-1], 2)
        if price is None:
            return None
        
        rsi = _rsi(closes)
        # Sanity check
        if not (10.0 <= rsi <= 98.0):
            print(f"  {symbol:<6} SKIP — RSI={rsi} fuera de rango válido", file=sys.stderr)
            return None
        
        # Información básica
        try:
            info = ticker.info or {}
        except Exception:
            info = {}
        
        # Detectar tipo de activo y sector
        asset_type = detect_asset_type(info)
        sector = detect_sector(info)
        
        # ─── TÉCNICOS ───────────────────────────────────────────────────────────
        macd = _macd_signal(closes)
        trend = _trend(closes)
        support, resistance = _support_resistance(closes)
        sma50 = round_safe(closes.rolling(50).mean().iloc[-1], 2)
        sma200_val = closes.rolling(200).mean().iloc[-1] if len(closes) >= 200 else None
        sma200 = round_safe(sma200_val, 2)
        
        volumes = hist["Volume"]
        vol_30d_avg = float(volumes.rolling(30).mean().iloc[-1]) if len(volumes) >= 30 else None
        volume_ratio = round_safe(float(volumes.iloc[-1]) / vol_30d_avg, 2) if vol_30d_avg and vol_30d_avg > 0 else None
        
        low_52w = float(closes.tail(252).min())
        high_52w = float(closes.tail(252).max())
        range_52w_pct = round_safe((price - low_52w) / (high_52w - low_52w) * 100, 1) if high_52w > low_52w else None
        
        atr_14 = _atr(hist["High"], hist["Low"], closes)
        
        # ─── FUNDAMENTALES BASE (Extraídos primero para permitir cálculos derivados) ─
        revenue = safe_get(info, "totalRevenue")
        revenue_growth = safe_get(info, "revenueGrowth")
        gross_margin = safe_get(info, "grossMargins")
        roe = safe_get(info, "returnOnEquity")
        
        # FCF - cálculo robusto (Requerido para Price/FCF derivado)
        fcf, fcf_margin, fcf_abs, operating_cash_flow, capital_expenditures = calculate_fcf(info)

        # ─── VALUACIÓN Y PARCHADO DE NULOS ──────────────────────────────────────
        pe = safe_get(info, "trailingPE")
        forward_pe = safe_get(info, "forwardPE")
        peg = safe_get(info, "trailingPegRatio") or safe_get(info, "pegRatio")
        ev_ebitda = safe_get(info, "enterpriseToEbitda")
        price_to_fcf = safe_get(info, "priceToFreeCashflows")
        
        market_cap = safe_get(info, "marketCap")
        dividend_rate = safe_get(info, "dividendRate")
        eps_trailing = safe_get(info, "trailingEps")

        # Fallback 1: Reconstrucción manual de Price to FCF
        if price_to_fcf is None and market_cap is not None:
            if fcf_abs and fcf_abs > 0:
                price_to_fcf = round_safe(market_cap / fcf_abs, 2)

        # Fallback 2: Reconstrucción manual de PEG Ratio (Evitando crecimientos negativos)
        if peg is None:
            pe_base = forward_pe or pe
            growth_rate = safe_get(info, "earningsQuarterlyGrowth") or revenue_growth
            if pe_base and growth_rate and growth_rate > 0:
                peg = round_safe(pe_base / (growth_rate * 100), 2)
        
        # ─── METRICAS POR TIPO DE ACTIVO ───────────────────────────────────────
        # Para REITs: intentar obtener AFFO o FFO
        affo = None
        if asset_type == "reit":
            affo = safe_get(info, "fundsFromOperations") or safe_get(info, "affo")
        
        # Para Financieros: Net Income Margin como alternativa
        net_income_margin = None
        if asset_type == "financial":
            net_income = safe_get(info, "netIncomeToCommon")
            if net_income is not None and revenue is not None and revenue > 0:
                net_income_margin = round_safe(net_income / revenue, 4)
        
        # Deuda/Equity
        debt_equity = safe_get(info, "debtToEquity")
        
        # Dividendos y Fallback 3 para Payout Ratio
        dividend_yield = safe_get(info, "dividendYield")
        payout_ratio = safe_get(info, "payoutRatio")
        
        if payout_ratio is None and dividend_rate is not None and eps_trailing:
            if eps_trailing > 0:
                payout_ratio = round_safe(dividend_rate / eps_trailing, 4)
        
        # ─── SHORT INTEREST ────────────────────────────────────────────────────
        short_ratio = safe_get(info, "shortRatio")
        short_float = safe_get(info, "shortPercentOfFloat")
        short_float_pct = safe_percent(short_float)
        
        # ─── ENTRY QUALITY ─────────────────────────────────────────────────────
        entry = _entry_quality(rsi, price, support, fcf_margin, revenue_growth, asset_type)
        
        # ─── EARNINGS ──────────────────────────────────────────────────────────
        earnings = _earnings(ticker)
        
        # ─── INSIDER ──────────────────────────────────────────────────────────
        insider = _insider(ticker)
        
        # ─── RELATIVE STRENGTH ─────────────────────────────────────────────────
        relative_strength_3m = None
        try:
            spy_hist = yf.Ticker("SPY").history(period="3mo", interval="1d")
            if not spy_hist.empty and len(closes) >= 63:
                spy_ret = (spy_hist["Close"].iloc[-1] / spy_hist["Close"].iloc[0] - 1) * 100
                ticker_ret = (closes.iloc[-1] / closes.iloc[-63] - 1) * 100
                relative_strength_3m = round_safe(ticker_ret - spy_ret, 2)
        except Exception:
            pass
        
        # ─── EPS REVISION ──────────────────────────────────────────────────────
        eps_revision = None
        try:
            qg = safe_get(info, "earningsQuarterlyGrowth")
            if qg is not None:
                eps_revision = "raising" if qg > 0.05 else "lowering" if qg < -0.05 else "stable"
        except Exception:
            pass
        
        # ─── CONSTRUIR RESULTADO ──────────────────────────────────────────────
        result = {
            "price": price,
            "asset_type": asset_type,
            "sector": sector,
            "technicals": {
                "trend": trend,
                "rsi": rsi,
                "macd": macd,
                "sma_50": sma50,
                "sma_200": sma200,
                "key_support": support,
                "key_resistance": resistance,
                "entry_quality": entry,
                "volume_ratio": volume_ratio,
                "range_52w_pct": range_52w_pct,
                "atr_14": atr_14,
            },
            "valuation": {
                "pe": pe,
                "forward_pe": forward_pe,
                "peg": peg,
                "ev_ebitda": ev_ebitda,
                "price_to_fcf": price_to_fcf,
            },
            "fundamentals": {
                "revenue_growth_yoy": revenue_growth,
                "gross_margin": gross_margin,
                "fcf_margin": fcf_margin,
                "fcf_abs": fcf_abs,
                "operating_cash_flow": operating_cash_flow,
                "capital_expenditures": capital_expenditures,
                "debt_equity": debt_equity,
                "roe": roe,
                "free_cashflow": fcf,
                "total_revenue": revenue,
                # Campos específicos por tipo de activo
                "affo": affo,
                "net_income_margin": net_income_margin,
                "dividend_yield": dividend_yield,
                "payout_ratio": payout_ratio,
            },
            "earnings": earnings,
            "insider_signal": insider,
            "short_interest": {
                "short_ratio": short_ratio,
                "short_float_pct": short_float_pct,
            },
            "relative_strength_3m": relative_strength_3m,
            "eps_revision": eps_revision,
        }
        
        return result
        
    except Exception as e:
        print(f"  {symbol:<6} ERROR — {str(e)[:50]}", file=sys.stderr)
        return None

# ─── Macro fetch ──────────────────────────────────────────────────────────────

def fetch_macro() -> Dict:
    try:
        data = yf.download(
            MACRO_TICKERS, period="6mo", interval="1d", progress=False
        )
        if isinstance(data.columns, pd.MultiIndex):
            data = data["Close"]
        elif "Close" in data.columns:
            data = data["Close"]
        
        latest = data.iloc[-1]
        vix = round_safe(latest.get("^VIX", 20), 2)
        tnx = round_safe(latest.get("^TNX", 4.3), 3)
        irx = round_safe(latest.get("^IRX", 3.0), 3)
        
        gspc = data["^GSPC"].dropna()
        spy_rsi = _rsi(gspc)
        
        # Fear & Greed
        vix_score = max(0.0, min(100.0, 100 - ((vix - 10) / 25 * 100)))
        fg_synthetic = round_safe((vix_score * 0.5) + (spy_rsi * 0.5), 1)
        fg = fg_synthetic
        fg_label = (
            "Extreme Fear" if fg < 25 else
            "Fear" if fg < 45 else
            "Neutral" if fg < 55 else
            "Greed" if fg < 75 else
            "Extreme Greed"
        )
        fg_source = "synthetic"
        try:
            req = urllib.request.urlopen(
                "https://api.alternative.me/fng/?limit=1", timeout=5
            )
            fng_data = json.loads(req.read().decode())["data"][0]
            fg = int(fng_data["value"])
            fg_label = fng_data["value_classification"]
            fg_source = "alternative.me"
        except Exception:
            pass
        
        # DXY
        dxy = None
        dxy_trend = "unknown"
        try:
            dxy_series = data["DX-Y.NYB"].dropna()
            if not dxy_series.empty:
                dxy = round_safe(dxy_series.iloc[-1], 2)
                dxy_20 = float(dxy_series.iloc[-20]) if len(dxy_series) >= 20 else dxy
                dxy_trend = "rising" if dxy > dxy_20 * 1.01 else "falling" if dxy < dxy_20 * 0.99 else "stable"
        except Exception:
            pass
        
        tnx_20 = float(data["^TNX"].iloc[-20]) if len(data) >= 20 else tnx
        tnx_trend = "rising" if tnx > tnx_20 * 1.05 else "falling" if tnx < tnx_20 * 0.95 else "stable"
        
        # Market regime
        sma50 = float(gspc.rolling(50).mean().iloc[-1])
        sma200 = float(gspc.rolling(200).mean().iloc[-1]) if len(gspc) >= 200 else None
        price = float(gspc.iloc[-1])
        if sma200 and price > sma50 > sma200:
            regime = "BULL"
        elif sma200 and price < sma50 < sma200:
            regime = "BEAR"
        else:
            regime = "MIXED"
        
        return {
            "vix": vix,
            "yield_10y": tnx,
            "yield_3m": irx,
            "yield_spread_10y_3m": round_safe(tnx - irx, 3),
            "spy_price": round_safe(gspc.iloc[-1], 2),
            "spy_rsi": spy_rsi,
            "fear_greed_value": fg,
            "fear_greed_index": fg,
            "fear_greed_label": fg_label,
            "fear_greed_synthetic": fg_synthetic,
            "fear_greed_source": fg_source,
            "yield_trend": tnx_trend,
            "dxy": dxy,
            "dxy_trend": dxy_trend,
            "market_regime": regime,
        }
    except Exception as e:
        return {"error": str(e)}

# ─── Candidate filter mejorado ─────────────────────────────────────────────────

def filter_candidates(stocks: Dict, top_n: int = 35) -> List[Dict]:
    QUALITY_RANK = {
        "excellent — oversold": 0,
        "excellent — oversold (ETF)": 0,
        "excellent — oversold (verify)": 1,
        "oversold — verify": 1,
        "oversold — verify fundamentals": 2,
        "good — near support": 2,
        "good — near support (ETF)": 2,
        "fair": 3,
        "fair (ETF)": 3,
        "oversold — fundamentals deteriorating": 4,
    }
    
    candidates = []
    for symbol, data in stocks.items():
        tech = data.get("technicals", {})
        rsi = tech.get("rsi", 100)
        entry = tech.get("entry_quality", "")
        asset_type = data.get("asset_type", "stock")
        
        if rsi > 70:
            continue
        if entry.startswith("poor"):
            continue
        
        quality_key = next((k for k in QUALITY_RANK if entry.startswith(k)), "fair")
        candidates.append({
            "symbol": symbol,
            "price": data.get("price"),
            "price_at_fetch": data.get("price"),
            "rsi": rsi,
            "trend": tech.get("trend"),
            "entry_quality": entry,
            "correlation_group": _SYMBOL_TO_GROUP.get(symbol, "other"),
            "asset_type": asset_type,
            "sector": data.get("sector"),
            "forward_pe": data.get("valuation", {}).get("forward_pe"),
            "peg": data.get("valuation", {}).get("peg"),
            "revenue_growth_yoy": data.get("fundamentals", {}).get("revenue_growth_yoy"),
            "fcf_margin": data.get("fundamentals", {}).get("fcf_margin"),
            "roe": data.get("fundamentals", {}).get("roe"),
            "gross_margin": data.get("fundamentals", {}).get("gross_margin"),
            "net_income_margin": data.get("fundamentals", {}).get("net_income_margin"),
            "affo": data.get("fundamentals", {}).get("affo"),
            "dividend_yield": data.get("fundamentals", {}).get("dividend_yield"),
            "payout_ratio": data.get("fundamentals", {}).get("payout_ratio"),
            "debt_equity": data.get("fundamentals", {}).get("debt_equity"),
            "earnings_days_away": data.get("earnings", {}).get("days_away"),
            "beat_streak": data.get("earnings", {}).get("beat_streak"),
            "insider_signal": data.get("insider_signal"),
            "volume_ratio": tech.get("volume_ratio"),
            "range_52w_pct": tech.get("range_52w_pct"),
            "atr_14": tech.get("atr_14"),
            "short_ratio": data.get("short_interest", {}).get("short_ratio"),
            "short_float_pct": data.get("short_interest", {}).get("short_float_pct"),
            "relative_strength_3m": data.get("relative_strength_3m"),
            "eps_revision": data.get("eps_revision"),
            "_sort_key": (QUALITY_RANK.get(quality_key, 2), rsi),
        })
    
    candidates.sort(key=lambda x: x["_sort_key"])
    for c in candidates:
        del c["_sort_key"]
    
    return candidates[:top_n]

def build_correlation_warnings(candidates: List[Dict]) -> List[Dict]:
    from collections import defaultdict
    group_members: Dict[str, List[str]] = defaultdict(list)
    for c in candidates:
        group = c.get("correlation_group", "other")
        group_members[group].append(c["symbol"])
    
    warnings_out = []
    for group, symbols in group_members.items():
        if len(symbols) > MAX_PICKS_PER_GROUP:
            ranked = sorted(
                [c for c in candidates if c.get("correlation_group") == group],
                key=lambda x: x["rsi"],
            )
            keep = [r["symbol"] for r in ranked[:MAX_PICKS_PER_GROUP]]
            cut = [r["symbol"] for r in ranked[MAX_PICKS_PER_GROUP:]]
            warnings_out.append({
                "group": group,
                "count": len(symbols),
                "max_allowed": MAX_PICKS_PER_GROUP,
                "all_candidates": symbols,
                "suggested_keep": keep,
                "suggested_cut": cut,
                "message": (
                    f"Over-concentration: {len(symbols)} '{group}' candidates "
                    f"({', '.join(symbols)}). "
                    f"Recommend keeping max {MAX_PICKS_PER_GROUP}: {', '.join(keep)}. "
                    f"Consider cutting: {', '.join(cut)}."
                ),
            })
    return warnings_out

# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    
    if "--watchlist" in args:
        idx = args.index("--watchlist")
        watchlist_names = []
        individual = []
        after = args[idx + 1:]
        for token in after:
            if token in WATCHLISTS:
                watchlist_names.append(token)
            elif not token.startswith("--"):
                individual.append(token.upper())
            else:
                break
        before = [t.upper() for t in args[:idx] if not t.startswith("--")]
        tickers_set = list(dict.fromkeys(
            before +
            [t for name in watchlist_names for t in WATCHLISTS[name]] +
            individual
        ))
        if not tickers_set:
            print(f"[pre_fetch] Unknown watchlist(s). Available: {', '.join(WATCHLISTS)}")
            sys.exit(1)
        tickers = tickers_set
        label = f"watchlist={'+'.join(watchlist_names) or 'none'}"
    elif args:
        tickers = [t.upper() for t in args]
        label = "custom"
    else:
        tickers = DEFAULT_TICKERS
        label = "default"
    
    print(f"[pre_fetch] {datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')} — {label} — fetching {len(tickers)} tickers + macro (parallel)")
    
    stocks = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_stock, symbol): symbol for symbol in tickers}
        for future in as_completed(futures):
            symbol = futures[future]
            result = future.result()
            if result:
                t = result["technicals"]
                v = result["valuation"]
                f = result["fundamentals"]
                asset_type = result.get("asset_type", "stock")
                print(f"  {symbol:<6} {asset_type:<8} RSI={t['rsi']}  {t['trend']:<10}  PEG={v['peg']}  FCF={f.get('fcf_margin', 'N/A')}")
                stocks[symbol] = result
            else:
                print(f"  {symbol:<6} SKIP — no data")
    
    print("[pre_fetch] Fetching macro...", end=" ", flush=True)
    macro = fetch_macro()
    if "error" in macro:
        print(f"ERROR: {macro['error']}")
    else:
        fg_src = macro.get('fear_greed_source', 'synthetic')
        print(f"VIX={macro['vix']}  F&G={macro['fear_greed_index']} ({macro['fear_greed_label']}) [{fg_src}]  regime={macro['market_regime']}")
    
    candidates = filter_candidates(stocks)
    print(f"[pre_fetch] Screened candidates: {len(candidates)}/{len(stocks)} pass RSI+entry filter")
    
    # Estadísticas por tipo de activo
    asset_counts = {}
    for c in candidates:
        asset_type = c.get("asset_type", "stock")
        asset_counts[asset_type] = asset_counts.get(asset_type, 0) + 1
    if asset_counts:
        print(f"[pre_fetch] Candidate types: {', '.join(f'{k}:{v}' for k,v in asset_counts.items())}")
    
    for c in candidates[:10]:
        print(f"  {c['symbol']:<6}  RSI={c['rsi']}  {c['entry_quality']}")
    
    correlation_warnings = build_correlation_warnings(candidates)
    if correlation_warnings:
        print(f"[pre_fetch] ⚠ Correlation warnings ({len(correlation_warnings)}):")
        for w in correlation_warnings:
            print(f"  {w['message']}")
    else:
        print("[pre_fetch] ✓ No correlation over-concentration detected")
    
    fetch_ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    
    prices_snapshot = {
        symbol: {
            "price": data["price"],
            "fetched_at": fetch_ts,
            "asset_type": data.get("asset_type", "stock"),
        }
        for symbol, data in stocks.items()
    }
    
    output = {
        "generated_at": fetch_ts,
        "tickers_fetched": list(stocks.keys()),
        "prices_snapshot": prices_snapshot,
        "candidates": candidates,
        "correlation_warnings": correlation_warnings,
        "stocks": stocks,
        "macro": macro,
    }
    
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    
    print(f"[pre_fetch] ✓ Written → {OUTPUT_PATH}  ({os.path.getsize(OUTPUT_PATH):,} bytes)")
    
    history_dir = os.path.join(SKILL_ROOT, "output", "history")
    if os.path.isdir(history_dir):
        history_files = sorted(glob.glob(os.path.join(history_dir, "*.json")))
        for old_file in history_files[:-30]:
            os.remove(old_file)
            print(f"[pre_fetch] Pruned old history: {os.path.basename(old_file)}")

if __name__ == "__main__":
    main()