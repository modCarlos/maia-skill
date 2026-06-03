#!/usr/bin/env python3
"""
portfolio_agent.py — Analiza el portfolio con Ollama (sin cloud).

Lee data/portfolio_market.json (generado por portfolio_fetch.py),
llama a Ollama y produce el JSON de estrategia que espera
write_portfolio_report.py.

Uso: python3 tools/portfolio_agent.py
Output: JSON a stdout → piped a write_portfolio_report.py
"""

import json
import sys
import os
import re
import requests
from pathlib import Path
from datetime import datetime, timezone

# ─── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL  = "http://localhost:11434/api/chat"
MODEL       = os.getenv("MAIA_MODEL", "qwen2.5:14b")
MAX_RETRIES = 3

REPO         = Path(__file__).parent.parent
MARKET_FILE  = REPO / "data" / "portfolio_market.json"
PORTFOLIO_FILE = REPO / "data" / "portfolio.json"

# ─── Schema requerido por write_portfolio_report.py ──────────────────────────
SYSTEM_PROMPT = """You are a professional portfolio manager analyzing an investment portfolio.

Your task: analyze each position using the provided market data and produce a complete portfolio strategy report as a single JSON object.

CRITICAL RULES:
1. Output ONLY valid JSON — no markdown, no text outside the JSON
2. Analyze EVERY position — do not skip any
3. Use the real P&L, RSI, and fundamental data provided — do not invent numbers
4. Be specific in reasoning — reference actual data points (price, RSI, P&L%, analyst target)
5. Action must be one of: HOLD, TRIM, EXIT, ADD
6. Urgency must be one of: HIGH, MEDIUM, LOW
7. Position health must be one of: STRONG, MODERATE, WEAK
8. Thesis status must be one of: ACTIVE, DETERIORATING, INVALIDATED

REQUIRED JSON STRUCTURE:
{
  "generated_at": "<ISO 8601 datetime>",
  "portfolio_summary": {
    "total_positions": <integer>,
    "total_cost": <number — sum of buy_price × quantity>,
    "total_current_value": <number — sum of current_price × quantity>,
    "total_pnl": <number — total_current_value - total_cost>,
    "total_pnl_pct": <number — (total_pnl / total_cost) × 100>,
    "overall_health": "STRONG|MODERATE|WEAK",
    "immediate_actions_needed": <integer — count of HIGH urgency positions>
  },
  "positions": [
    {
      "symbol": "<TICKER>",
      "action": "HOLD|TRIM|EXIT|ADD",
      "urgency": "HIGH|MEDIUM|LOW",
      "position_health": "STRONG|MODERATE|WEAK",
      "thesis_status": "ACTIVE|DETERIORATING|INVALIDATED",
      "reasoning": "<specific reasoning with data: P&L%, RSI, analyst target, key risk>"
    }
  ],
  "cross_position_insights": [
    "<observation about portfolio-level risk, correlation, or opportunity>"
  ],
  "priority_attention": [
    "<ticker or action that needs immediate attention>"
  ],
  "warnings": [
    "This analysis is for informational purposes only and does not constitute financial advice."
  ]
}"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

MAX_POSITIONS_PER_BATCH = 20  # modelo local no puede manejar más de ~20 posiciones a la vez


def prioritize_portfolio(portfolio: list, market_data: dict, max_pos: int = MAX_POSITIONS_PER_BATCH) -> list:
    """Prioriza las posiciones más críticas cuando el portfolio es muy grande.
    Orden: pérdidas grandes → RSI alto (sobrecomprado) → ganancias extremas → resto.
    """
    if len(portfolio) <= max_pos:
        return portfolio

    pos_by_sym = {p.get("symbol"): p for p in market_data.get("positions", [])}

    def urgency_score(entry):
        sym = entry.get("symbol", "")
        mkt = pos_by_sym.get(sym, {})
        pnl_pct = mkt.get("pnl_pct") or 0
        rsi = mkt.get("rsi_14") or 50
        buy_p = entry.get("buyPrice") or entry.get("buy_price") or 1
        qty = entry.get("quantity") or 1
        curr_p = mkt.get("current_price") or buy_p
        position_value = curr_p * qty

        # Puntuación de urgencia: mayor = más urgente
        score = 0
        if pnl_pct < -15: score += 100    # pérdida severa
        if pnl_pct < -5:  score += 50     # pérdida moderada
        if rsi > 75:       score += 60     # sobrecomprado, riesgo de caída
        if rsi < 30:       score += 40     # oversold, posible rebote
        if pnl_pct > 50:   score += 30     # ganancia grande — revisar si tomar ganancias
        score += position_value / 1000     # posiciones más grandes tienen más impacto
        return -score  # negativo para ordenar descending

    sorted_portfolio = sorted(portfolio, key=urgency_score)
    print(f"   ℹ️  Portfolio grande ({len(portfolio)} posiciones) — analizando top {max_pos} más críticas", file=sys.stderr)
    excluded = [e["symbol"] for e in sorted_portfolio[max_pos:]]
    print(f"   Excluidas (menor urgencia): {', '.join(excluded)}", file=sys.stderr)
    return sorted_portfolio[:max_pos]


def compress_portfolio(market_data: dict, portfolio: list) -> str:
    """Construye contexto comprimido del portfolio para el LLM."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Priorizar si hay demasiadas posiciones
    portfolio = prioritize_portfolio(portfolio, market_data)

    lines = [f"DATE: {today}", "", "=== PORTFOLIO POSITIONS ==="]

    positions = market_data.get("positions", [])
    pos_by_sym = {p.get("symbol"): p for p in positions}

    total_cost = 0
    total_value = 0

    for entry in portfolio:
        sym      = entry.get("symbol", "?")
        name     = entry.get("name", sym)
        sector   = entry.get("sector", "?")
        buy_p    = entry.get("buyPrice") or entry.get("buy_price", 0)
        qty      = entry.get("quantity", 0)
        buy_date = (entry.get("buyDate") or entry.get("buy_date", "?"))[:10]

        mkt = pos_by_sym.get(sym, {})
        curr_p   = mkt.get("current_price") or buy_p
        pnl_pct  = mkt.get("pnl_pct")
        pnl_amt  = mkt.get("pnl_amount") or round((curr_p - buy_p) * qty, 2)
        rsi      = mkt.get("rsi_14", "?")
        trend    = mkt.get("trend", "?")
        chg_1d   = mkt.get("change_pct_1d", "?")
        chg_30d  = mkt.get("change_pct_30d", "?")
        h52      = mkt.get("week_52_high")
        pct_h52  = mkt.get("pct_from_52w_high")
        fpe      = mkt.get("forward_pe")
        peg      = mkt.get("peg_ratio")
        analyst_t = mkt.get("analyst_target")
        analyst_u = mkt.get("analyst_upside")
        news_s   = mkt.get("news_sentiment", "neutral")
        headlines = mkt.get("news_headlines", [])
        altman   = (mkt.get("altman_z") or {}).get("zone")
        piotroski = (mkt.get("piotroski") or {}).get("score")
        rec_mean = mkt.get("recommendation_mean")

        cost_pos = round(buy_p * qty, 2)
        val_pos  = round(curr_p * qty, 2)
        total_cost  += cost_pos
        total_value += val_pos

        pnl_str = f"{pnl_pct:+.1f}%" if pnl_pct is not None else "?"
        analyst_str = f" analyst_target=${analyst_t}({analyst_u:+.1f}%)" if analyst_t and analyst_u else ""
        h52_str = f" from_52w_high={pct_h52:.1f}%" if pct_h52 else ""

        lines.append(f"\n{sym} — {name} ({sector})")
        lines.append(f"  buy=${buy_p} × {qty} = ${cost_pos}  |  now=${curr_p} → ${val_pos}  P&L={pnl_str} (${pnl_amt:+,.0f})  bought={buy_date}")
        lines.append(f"  RSI={rsi}  trend={trend}  1d={chg_1d}%  30d={chg_30d}%{h52_str}{analyst_str}")

        fundamentals = []
        if fpe:   fundamentals.append(f"fPE={fpe}")
        if peg:   fundamentals.append(f"PEG={peg}")
        if altman: fundamentals.append(f"altman={altman}")
        if piotroski: fundamentals.append(f"piotroski={piotroski}/9")
        if rec_mean: fundamentals.append(f"analyst_rec={rec_mean:.1f}(1=buy,5=sell)")
        if fundamentals:
            lines.append(f"  fundamentals: {' | '.join(fundamentals)}")

        lines.append(f"  news: {news_s}")
        for h in headlines[:1]:  # solo 1 headline para reducir tokens
            lines.append(f"    → {h[:80]}")

    # Portfolio totals
    total_pnl = round(total_value - total_cost, 2)
    total_pnl_pct = round(total_pnl / total_cost * 100, 2) if total_cost else 0
    lines.insert(2, f"PORTFOLIO TOTALS: cost=${total_cost:,.0f}  value=${total_value:,.0f}  P&L=${total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)")

    return "\n".join(lines)


def call_ollama(context: str, attempt: int) -> str:
    correction = ""
    if attempt > 1:
        correction = (
            f"\n\n⚠️ ATTEMPT {attempt}/{MAX_RETRIES}: Previous output was invalid. "
            "Output ONLY valid JSON. Analyze ALL positions shown. Do NOT truncate."
        )

    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": context + correction},
            ],
            "options": {"temperature": 0.2, "num_predict": 2500},
            "stream": False,
        },
        timeout=480,
    )
    resp.raise_for_status()
    result = resp.json()
    content = result.get("message", {}).get("content", "")
    if not content or not content.strip():
        # Mostrar respuesta completa para debug
        print(f"   ⚠️  Respuesta vacía de Ollama. done_reason={result.get('done_reason')} done={result.get('done')}", file=sys.stderr)
        print(f"   eval_count={result.get('eval_count')} prompt_eval_count={result.get('prompt_eval_count')}", file=sys.stderr)
        raise json.JSONDecodeError("Respuesta vacía del modelo", "", 0)
    return content


def repair_json(text: str) -> str:
    text = re.sub(r",\s*([}\]])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    return text


def extract_json(text: str) -> dict:
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    first = text.find("{")
    last  = text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(repair_json(text))


def fill_defaults(data: dict, portfolio: list) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data.setdefault("generated_at", now)
    data.setdefault("cross_position_insights", [])
    data.setdefault("priority_attention", [])
    data.setdefault("warnings", [
        "This analysis is for informational purposes only and does not constitute financial advice."
    ])
    return data


def validate(data: dict) -> list[str]:
    errors = []
    if not data.get("portfolio_summary"):
        errors.append("Missing portfolio_summary")
    if not isinstance(data.get("positions"), list) or len(data["positions"]) == 0:
        errors.append("positions is empty")
    else:
        for p in data["positions"][:2]:
            if not p.get("symbol"):
                errors.append("Position missing symbol")
            if not p.get("reasoning"):
                errors.append(f"Position {p.get('symbol','?')} missing reasoning")
    return errors


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not PORTFOLIO_FILE.exists():
        print(f"ERROR: {PORTFOLIO_FILE} not found.", file=sys.stderr)
        sys.exit(1)
    if not MARKET_FILE.exists():
        print(f"ERROR: {MARKET_FILE} not found. Run portfolio_fetch.py first.", file=sys.stderr)
        sys.exit(1)

    portfolio   = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    market_data = json.loads(MARKET_FILE.read_text(encoding="utf-8"))

    print(f"🤖 Portfolio Agent | modelo: {MODEL} | {len(portfolio)} posiciones", file=sys.stderr)

    context = compress_portfolio(market_data, portfolio)
    print(f"   Contexto: {len(context):,} chars", file=sys.stderr)

    # Si el contexto es demasiado largo, truncar noticias para caber en context window
    if len(context) > 12000:
        print(f"   ⚠️  Contexto largo — truncando headlines para reducir tokens", file=sys.stderr)
        context = context[:12000] + "\n[...context truncated to fit model context window...]"

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"   Intento {attempt}/{MAX_RETRIES}...", file=sys.stderr)
        try:
            raw    = call_ollama(context, attempt)
            data   = extract_json(raw)
            data   = fill_defaults(data, portfolio)
            errors = validate(data)

            if errors:
                print(f"   ⚠️  Validación: {errors}", file=sys.stderr)
                if attempt < MAX_RETRIES:
                    continue
                # Último intento — guardar output parcial si hay positions
                if data.get("positions"):
                    print("   ⚠️  Guardando output parcial...", file=sys.stderr)
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                    sys.exit(0)
                sys.exit(1)

            n = len(data.get("positions", []))
            print(f"   ✅ Análisis completo — {n} posiciones", file=sys.stderr)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return

        except json.JSONDecodeError as e:
            print(f"   ⚠️  JSON inválido (intento {attempt}): {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                sys.exit(1)
        except requests.RequestException as e:
            print(f"   ❌ Error Ollama: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
