#!/usr/bin/env python3
"""
mega_agent.py — MegaAgent local usando Ollama.

Lee los archivos de datos generados por los tools de pre-fetch,
construye un contexto comprimido, llama a Ollama y genera el
reporte completo en el schema que espera write_report.py.

Uso: python3 tools/mega_agent.py [conservative|moderate|aggressive]
Output: JSON completo a stdout (piped a write_report.py)
"""

import json
import sys
import os
import re
import requests
from pathlib import Path
from datetime import datetime, timezone

# Forzar UTF-8 en stdout/stderr para compatibilidad con Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ─── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/chat"  # native API — más estable que /v1
MODEL        = os.getenv("MAIA_MODEL", "qwen2.5:14b")
MAX_RETRIES  = 3
# Aumentar MAIA_NUM_PREDICT en GPU potente (ej: export MAIA_NUM_PREDICT=4000)
NUM_PREDICT  = int(os.getenv("MAIA_NUM_PREDICT", "1500"))
# Aumentar MAIA_TIMEOUT para modelos grandes como 32b (ej: export MAIA_TIMEOUT=900)
TIMEOUT      = int(os.getenv("MAIA_TIMEOUT", "900"))

REPO = Path(__file__).parent.parent
DATA_DIR    = REPO / "data"
MARKET_CTX  = DATA_DIR / "market_context.json"
NEWS_CTX    = DATA_DIR / "news_context.json"
SEC_CTX     = DATA_DIR / "sec_risk_context.json"
BACKTEST_CTX = DATA_DIR / "backtest_summary.json"

# ─── Schema requerido por write_report.py ────────────────────────────────────
REQUIRED_TOP = [
    "brand", "creator", "generated_at", "risk_profile",
    "executive_summary", "macro_environment", "portfolio_allocation",
    "cross_sector_insights", "risk_adjusted_picks", "historical_accuracy",
    "warnings", "sectors",
]
REQUIRED_MACRO = [
    "summary", "interest_rate_outlook", "inflation_outlook",
    "geopolitical_risk", "key_factors",
]
REQUIRED_PICK = [
    "rank", "name", "symbol", "sector", "confidence", "risk_score",
    "risk_adjusted_score", "recommendation", "reasoning", "position_size",
    "entry_price", "stop_loss", "target_12m", "risk_reward_ratio",
    "thesis", "thesis_invalidators", "thesis_status",
]

# ─── System prompt para el MegaAgent ─────────────────────────────────────────
SYSTEM_PROMPT = """You are a professional investment analyst for Tododeia, a financial research system.

Your task: analyze the provided market data and produce a complete investment report as a single JSON object.

CRITICAL RULES:
1. Output ONLY valid JSON — no markdown code blocks, no explanation text, nothing outside the JSON
2. The JSON must have ALL required top-level fields
3. Include 10-13 investment picks in risk_adjusted_picks — aim for the upper end of this range
4. Each pick must have ALL required fields with correct data types
5. Use the real price data from MARKET_CONTEXT — do not invent numbers
6. Adapt position sizes and asset mix to the RISK_PROFILE

RECOMMENDATION DECISION RULES (apply these before assigning recommendation):
- entry_quality=excellent AND analyst_upside > 20% AND RSI < 45 → recommend ADD (not HOLD)
- entry_quality=good AND analyst_upside > 15% AND no insider selling → recommend ADD
- entry_quality=fair OR RSI > 65 OR insider selling detected → HOLD is appropriate
- Only use TRIM if RSI > 70 AND price is near 52-week high AND analyst_upside < 5%
- When in doubt between ADD and HOLD with strong fundamentals, choose ADD
- Do NOT default to HOLD simply because macro environment has uncertainty — all markets have uncertainty

REQUIRED JSON STRUCTURE:
{
  "brand": "Tododeia",
  "creator": "@quebert",
  "generated_at": "<ISO 8601 datetime>",
  "risk_profile": "<risk profile>",
  "executive_summary": "<2-3 sentence market narrative with specific data points>",
  "macro_environment": {
    "summary": "<macro context with VIX, Fear&Greed if available>",
    "interest_rate_outlook": "stable|rising|falling",
    "inflation_outlook": "stable|rising|falling",
    "geopolitical_risk": "low|moderate|high",
    "key_factors": ["<factor 1>", "<factor 2>", "<factor 3>"]
  },
  "portfolio_allocation": {
    "stocks": <number 0-100>,
    "materials": <number 0-100>,
    "cash": <number 0-100>
  },
  "cross_sector_insights": [
    {"insight": "<observation>", "implication": "<action implication>"}
  ],
  "risk_adjusted_picks": [
    {
      "rank": <integer 1-13>,
      "name": "<company full name>",
      "symbol": "<TICKER>",
      "sector": "<sector name>",
      "confidence": <number 1-10>,
      "risk_score": <number 1-10, lower is safer>,
      "risk_adjusted_score": <number 1-10>,
      "recommendation": "ADD|HOLD|TRIM",
      "reasoning": "<specific reasoning with data from MARKET_CONTEXT>",
      "position_size": <percentage of portfolio as number>,
      "entry_price": <number — use current_price from data>,
      "stop_loss": <number>,
      "target_12m": <number>,
      "risk_reward_ratio": <number>,
      "thesis": "<specific catalyst with date/metric>",
      "thesis_invalidators": "<specific condition that breaks thesis>",
      "thesis_status": "NEW|ACTIVE|CARRY_FORWARD"
    }
  ],
  "historical_accuracy": {
    "note": "First run — no historical data available",
    "sessions": 0
  },
  "warnings": ["This report is for informational purposes only and does not constitute financial advice."],
  "sectors": {
    "stocks": {"count": <number>, "avg_confidence": <number>},
    "materials": {"count": <number>, "avg_confidence": <number>}
  }
}"""


# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_json_safe(path: Path, label: str) -> dict | list | None:
    if not path.exists():
        print(f"  ⚠️  {label} not found ({path}) — skipping", file=sys.stderr)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ⚠️  {label} is invalid JSON: {e}", file=sys.stderr)
        return None


def compress_macro(macro: dict) -> str:
    """Bloque de contexto macro — VIX, yields, DXY, F&G, regime."""
    if not macro or "error" in macro:
        return "MACRO: unavailable"
    vix    = macro.get("vix", "?")
    regime = macro.get("market_regime", "?")
    tnx    = macro.get("yield_10y", "?")
    tnx_tr = macro.get("yield_trend", "?")
    spread = macro.get("yield_spread_10y_3m", "?")
    spy_p  = macro.get("spy_price", "?")
    spy_r  = macro.get("spy_rsi", "?")
    dxy    = macro.get("dxy", "?")
    dxy_tr = macro.get("dxy_trend", "?")

    # Fear & Greed: si alternative.me y el sintético difieren > 30 puntos,
    # hay contradicción (alternative.me rastrea crypto, no stocks). Usar sintético.
    fg_ext     = macro.get("fear_greed_index", "?")
    fg_synth   = macro.get("fear_greed_synthetic", fg_ext)
    fg_source  = macro.get("fear_greed_source", "synthetic")
    fg_lbl     = macro.get("fear_greed_label", "")
    if fg_source == "alternative.me" and isinstance(fg_ext, (int, float)) and isinstance(fg_synth, (int, float)):
        if abs(fg_ext - fg_synth) > 30:
            fg     = fg_synth
            fg_lbl = ("Extreme Fear" if fg < 25 else "Fear" if fg < 45 else
                      "Neutral" if fg < 55 else "Greed" if fg < 75 else "Extreme Greed")
            fg_source = "synthetic(⚠ alt.me diverged)"
        else:
            fg = fg_ext
    else:
        fg = fg_synth
    return (
        f"MACRO: VIX={vix}  F&G={fg}({fg_lbl})[{fg_source}]  regime={regime}\n"
        f"  yields: 10Y={tnx}% ({tnx_tr}) spread={spread}%\n"
        f"  SPY=${spy_p} RSI={spy_r}  DXY={dxy}({dxy_tr})"
    )


def compress_market(data: dict) -> str:
    """Convierte market_context.json en un bloque comprimido legible para el LLM.
    Limita a top 10 candidatos con todos los campos relevantes."""
    if not data:
        return "MARKET_CONTEXT: unavailable\n"

    # Macro block
    macro_block = compress_macro(data.get("macro", {}))

    # Correlation warnings
    corr_warnings = data.get("correlation_warnings", [])
    corr_block = ""
    if corr_warnings:
        msgs = [w.get("message", "") for w in corr_warnings[:3]]
        corr_block = "\nCORRELATION_LIMITS:\n" + "\n".join(f"  ⚠ {m}" for m in msgs)

    # Top 10 candidates with all relevant fields
    candidates = data.get("candidates") or data.get("screened_candidates") or []
    lines = ["SCREENED_CANDIDATES (top 10, sorted by entry quality):"]
    for c in candidates[:10]:
        sym      = c.get("symbol", "?")
        price    = c.get("price") or c.get("price_at_fetch", "?")
        rsi      = c.get("rsi", "?")
        trend    = c.get("trend", "?")
        entry_q  = c.get("entry_quality", "?")
        grp      = c.get("correlation_group", "?")
        fpe      = c.get("forward_pe")
        peg      = c.get("peg")
        fcf      = c.get("fcf_margin")
        rev_g    = c.get("revenue_growth_yoy")
        earn_d   = c.get("earnings_days_away")
        beat_s   = c.get("beat_streak", 0)
        insider  = c.get("insider_signal", "neutral")
        vol_r    = c.get("volume_ratio")
        rng_52   = c.get("range_52w_pct")
        short_f  = c.get("short_float_pct")
        rs3m     = c.get("relative_strength_3m")
        eps_rev  = c.get("eps_revision")
        atr_14   = c.get("atr_14")

        # Build compact one-liner
        atr_str = f"  ATR={atr_14}" if atr_14 is not None else ""
        parts = [f"  {sym:<6} ${price}  RSI={rsi}  {trend:<10}  entry={entry_q}{atr_str}  group={grp}"]

        valuation = []
        if fpe:  valuation.append(f"fPE={fpe}")
        if peg:  valuation.append(f"PEG={peg}")
        if fcf:  valuation.append(f"FCF={round(fcf*100,1)}%")
        if rev_g: valuation.append(f"revG={round(rev_g*100,1)}%")
        if valuation:
            parts.append("    val: " + "  ".join(valuation))

        signals = []
        if earn_d is not None: signals.append(f"earnings={earn_d}d(streak={beat_s})")
        if insider != "neutral": signals.append(f"insider={insider}")
        if vol_r and vol_r > 1.3: signals.append(f"vol={vol_r}x")
        if short_f and short_f > 10: signals.append(f"short={short_f}%")
        if rs3m is not None: signals.append(f"RS3m={rs3m:+.1f}%")
        if eps_rev and eps_rev != "stable": signals.append(f"epsRev={eps_rev}")
        if rng_52 is not None: signals.append(f"52wk={rng_52}%")
        if signals:
            parts.append("    signals: " + "  ".join(signals))

        lines.append("\n".join(parts))

    return f"{macro_block}\n{corr_block}\n\n" + "\n".join(lines)


def compress_news(data: dict, top_symbols: list[str] | None = None) -> str:
    """Incluye noticias solo de los tickers seleccionados (reduce tokens)."""
    if not data:
        return "NEWS_CONTEXT: unavailable\n"
    lines = ["=== NEWS_CONTEXT ==="]
    news_items = data.get("news", {})

    # Filtrar solo los top candidatos para reducir tokens
    if top_symbols:
        filtered = {k: v for k, v in news_items.items() if k in top_symbols}
    else:
        filtered = dict(list(news_items.items())[:12])

    for sym, info in filtered.items():
        if isinstance(info, dict):
            sentiment  = info.get("sentiment", {}).get("label", "?")
            headlines  = [n.get("title", "")[:90] for n in info.get("key_news", [])[:2]]
            analyst    = info.get("analyst_recommendation", "—")
            target     = info.get("analyst_target")
            insider    = info.get("insider", {})
            ins_signal = insider.get("signal", "none")

            meta = [f"sentiment={sentiment}", f"analyst={analyst}"]
            if target:  meta.append(f"target=${target}")
            if ins_signal != "none": meta.append(f"insider={ins_signal}")
            lines.append(f"  {sym}: " + "  ".join(meta))
            for h in headlines:
                lines.append(f"    → {h}")
    return "\n".join(lines)


def compress_sec(data: dict | list, top_symbols: list[str] | None = None) -> str:
    if not data:
        return "SEC_CONTEXT: unavailable\n"
    lines = ["=== SEC_RISK_CONTEXT ==="]
    items = data if isinstance(data, list) else data.get("risks", data.get("results", []))
    for item in items:
        sym = item.get("symbol") or item.get("ticker", "?")
        if top_symbols and sym not in top_symbols:
            continue
        risks = item.get("risks", item.get("key_risks", []))
        risk_str = " | ".join(str(r)[:70] for r in risks[:2])
        lines.append(f"  {sym}: {risk_str}")
        if len(lines) > 12:
            break
    return "\n".join(lines)


def build_context(risk_profile: str) -> str:
    market   = load_json_safe(MARKET_CTX,   "market_context")
    news     = load_json_safe(NEWS_CTX,     "news_context")
    sec      = load_json_safe(SEC_CTX,      "sec_risk_context")
    backtest = load_json_safe(BACKTEST_CTX, "backtest_summary")

    # Extraer los símbolos top para filtrar noticias/SEC
    top_symbols = []
    if market:
        candidates = market.get("candidates") or market.get("screened_candidates") or []
        top_symbols = [c.get("symbol") for c in candidates[:10] if c.get("symbol")]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    parts = [
        f"DATE: {today}",
        f"RISK_PROFILE: {risk_profile}",
        "",
        compress_market(market or {}),
        "",
        compress_news(news or {}, top_symbols),
        "",
        compress_sec(sec or {}, top_symbols),
    ]

    # Inyectar backtest solo cuando hay suficientes sesiones (evita ruido con N=1)
    if backtest:
        prompt_block = backtest.get("prompt_block", "")
        if prompt_block:
            parts += ["", prompt_block]

    # Inyectar holdings del portfolio para coherencia screener↔portfolio
    portfolio_path = REPO / "data" / "portfolio.json"
    if portfolio_path.exists():
        try:
            port_raw = json.loads(portfolio_path.read_text(encoding="utf-8"))
            held = sorted(set(
                (e.get("symbol") or "").upper().strip()
                for e in port_raw
                if (e.get("symbol") or "").strip()
            ))
            if held:
                parts += [
                    "",
                    "ALREADY HELD IN PORTFOLIO: " + " ".join(held),
                    "→ Held tickers: use thesis_status=CARRY_FORWARD. Still apply ADD/HOLD/TRIM rules normally.",
                ]
        except Exception:
            pass

    return "\n".join(parts)


def call_ollama(context: str, attempt: int) -> str:
    correction = ""
    if attempt > 1:
        correction = (
            f"\n\n⚠️ ATTEMPT {attempt}/{MAX_RETRIES}: Previous output was invalid. "
            "You MUST output ONLY a valid JSON object. "
            "Include ALL required fields. Do NOT truncate. Do NOT wrap in markdown."
        )

    # Usar la API nativa de Ollama (/api/chat) — más estable que el endpoint OpenAI
    resp = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": context + correction},
            ],
            "options": {
                "temperature": 0.2,
                "num_predict": NUM_PREDICT,
                "num_ctx": int(os.getenv("MAIA_NUM_CTX", "8192")),  # reducir a 4096 en modelos 32b con poca VRAM
            },
            "stream": False,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    content = result.get("message", {}).get("content", "")
    if not content or not content.strip():
        print(f"   ⚠️  Respuesta vacía. done_reason={result.get('done_reason')} eval_count={result.get('eval_count')} prompt_eval_count={result.get('prompt_eval_count')}", file=sys.stderr)
        raise json.JSONDecodeError("Respuesta vacía del modelo", "", 0)
    return content


def repair_json(text: str) -> str:
    """Repara errores comunes de JSON generados por modelos locales."""
    # Trailing commas antes de } o ]  →  el error más común
    text = re.sub(r",\s*([}\]])", r"\1", text)
    # Comas dobles
    text = re.sub(r",\s*,", ",", text)
    # Comillas simples → dobles (cuando el modelo usa Python-style)
    # Solo si no hay ya dobles alrededor
    text = re.sub(r"(?<![\\])'([^']*)'(?=\s*:)", r'"\1"', text)
    # Newlines literales dentro de strings (rompen JSON)
    # Reemplazar \n real dentro de valores string por \\n
    def fix_newlines_in_strings(m):
        return m.group(0).replace('\n', '\\n').replace('\r', '\\r')
    text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_newlines_in_strings, text, flags=re.DOTALL)
    return text


def extract_json(text: str) -> dict:
    """Extrae y repara JSON del output del modelo."""
    # Quitar bloques ```json ... ```
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)

    # Encontrar el primer { y el último }
    first = text.find("{")
    last  = text.rfind("}")
    if first >= 0 and last > first:
        text = text[first:last + 1]

    # Intento 1: JSON directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Intento 2: reparar errores comunes
    try:
        return json.loads(repair_json(text))
    except json.JSONDecodeError:
        pass

    # Intento 3: truncado — buscar el último pick completo y cerrar el JSON
    # Encuentra la última coma de pick completo y cierra el array
    last_complete = text.rfind("},\n    {")
    if last_complete == -1:
        last_complete = text.rfind("},\n  {")
    if last_complete > 0:
        truncated = text[:last_complete + 1]  # hasta el último pick completo
        # Cerrar arrays y objeto abiertos
        open_brackets = truncated.count("[") - truncated.count("]")
        open_braces   = truncated.count("{") - truncated.count("}")
        truncated += "]" * open_brackets + "}" * open_braces
        try:
            return json.loads(repair_json(truncated))
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError("No se pudo parsear ni reparar el JSON", text, 0)


def fill_defaults(data: dict, risk_profile: str) -> dict:
    """Rellena campos de metadata que el modelo local suele omitir."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data.setdefault("brand", "Tododeia")
    data.setdefault("creator", "@quebert")
    data.setdefault("generated_at", now)
    data.setdefault("risk_profile", risk_profile)
    # Poblar historical_accuracy desde backtest_summary si existe
    if not data.get("historical_accuracy") or data.get("historical_accuracy", {}).get("sessions", 0) == 0:
        backtest = load_json_safe(BACKTEST_CTX, "backtest_summary")
        if backtest and backtest.get("sessions_evaluated", 0) > 0:
            stats_30 = backtest.get("by_horizon", {}).get("30", {})
            hr = stats_30.get("hit_rate")
            avg_r = stats_30.get("avg_return_pct")
            data["historical_accuracy"] = {
                "sessions": backtest["sessions_evaluated"],
                "picks_evaluated": backtest.get("picks_evaluated", 0),
                "hit_rate_30d": hr,
                "avg_return_30d_pct": avg_r,
                "note": (
                    f"{backtest['sessions_evaluated']} sessions evaluated | "
                    f"30d hit_rate={f'{hr*100:.0f}%' if hr is not None else 'N/A'} | "
                    f"avg_return={f'{avg_r:+.1f}%' if avg_r is not None else 'N/A'}"
                ),
            }
        else:
            data.setdefault("historical_accuracy", {
                "note": "First run — no historical data available",
                "sessions": 0,
            })
    data.setdefault("warnings", [
        "This report is for informational purposes only and does not constitute financial advice."
    ])
    data.setdefault("cross_sector_insights", [])

    # Construir sectors a partir de los picks si falta
    if not data.get("sectors"):
        picks = data.get("risk_adjusted_picks", [])
        sector_map: dict = {}
        for p in picks:
            s = p.get("sector", "unknown")
            sector_map.setdefault(s, {"count": 0, "confidences": []})
            sector_map[s]["count"] += 1
            if "confidence" in p:
                sector_map[s]["confidences"].append(float(p["confidence"]))
        data["sectors"] = {
            s: {
                "count": v["count"],
                "avg_confidence": round(sum(v["confidences"]) / len(v["confidences"]), 1)
                if v["confidences"] else 0
            }
            for s, v in sector_map.items()
        }
        if not data["sectors"]:
            data["sectors"] = {"stocks": {"count": 0, "avg_confidence": 0}}

    return data


def validate(data: dict) -> list[str]:
    """Solo valida campos críticos — los metadata se rellenan con fill_defaults."""
    errors = []

    # Críticos: sin estos el reporte no tiene valor
    if not data.get("executive_summary"):
        errors.append("Missing: 'executive_summary'")

    macro = data.get("macro_environment", {})
    if not isinstance(macro, dict) or not macro:
        errors.append("Missing: 'macro_environment'")

    picks = data.get("risk_adjusted_picks", [])
    if not isinstance(picks, list) or len(picks) == 0:
        errors.append("risk_adjusted_picks is empty or missing")
    else:
        # Solo verificar que los picks tienen ticker/symbol y thesis
        for i, pick in enumerate(picks[:3]):
            sym = pick.get("symbol") or pick.get("ticker", f"#{i+1}")
            if not sym:
                errors.append(f"Pick #{i+1} has no symbol/ticker")
            if not pick.get("thesis") and not pick.get("reasoning"):
                errors.append(f"Pick {sym} has no thesis or reasoning")

    return errors


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    risk_profile = sys.argv[1] if len(sys.argv) > 1 else "moderate"
    print(f"🤖 MegaAgent local | modelo: {MODEL} | perfil: {risk_profile}", file=sys.stderr)

    context = build_context(risk_profile)
    print(f"   Contexto construido ({len(context):,} chars)", file=sys.stderr)

    # Truncar contexto si es demasiado largo para el context window del modelo
    if len(context) > 6000:
        print(f"   ⚠️  Contexto largo ({len(context):,} chars) — truncando a 6,000", file=sys.stderr)
        context = context[:6000] + "\n[...context truncated to fit model context window...]"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"   Intento {attempt}/{MAX_RETRIES}...", file=sys.stderr)
        try:
            raw    = call_ollama(context, attempt)
            data   = extract_json(raw)
            data   = fill_defaults(data, risk_profile)
            errors = validate(data)

            if errors:
                last_error = errors
                print(f"   ⚠️  Validación ({attempt}): {errors[:3]}", file=sys.stderr)
                if attempt < MAX_RETRIES:
                    continue
                # En el último intento, si hay picks, guardar igual con advertencia
                if data.get("risk_adjusted_picks"):
                    print("   ⚠️  Guardando output parcialmente válido...", file=sys.stderr)
                    print(json.dumps(data, indent=2, ensure_ascii=False))
                    sys.exit(0)
                sys.exit(1)

            picks_count = len(data.get("risk_adjusted_picks", []))
            print(f"   ✅ JSON válido — {picks_count} picks generados", file=sys.stderr)
            print(json.dumps(data, indent=2, ensure_ascii=False))
            return

        except json.JSONDecodeError as e:
            last_error = str(e)
            print(f"   ⚠️  JSON inválido (intento {attempt}): {e}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                print("   ❌ No se pudo parsear JSON tras todos los reintentos.", file=sys.stderr)
                sys.exit(1)

        except requests.RequestException as e:
            print(f"   ❌ Error conectando a Ollama: {e}", file=sys.stderr)
            print("   Verifica: brew services start ollama  (o: ollama serve)", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
