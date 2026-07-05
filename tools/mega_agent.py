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
import subprocess
import shutil
from pathlib import Path
from datetime import datetime, timezone

# Forzar UTF-8 en stdout/stderr para compatibilidad con Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# ─── Config ───────────────────────────────────────────────────────────────────
OLLAMA_URL   = "http://localhost:11434/api/chat"  # native API — más estable que /v1
MODEL        = os.getenv("MAIA_MODEL", "maia-agent")
_MAX_RETRIES  = 3

# ── Ajustes por modelo (7B / 8B / R1) ───────────────────────────────────────
_MODEL_LOWER = MODEL.lower()

_DEFAULT_NUM_PREDICT = "6000" if any(m in _MODEL_LOWER for m in ["deepseek-r1", "r1"]) else "4000"
NUM_PREDICT  = int(os.getenv("MAIA_NUM_PREDICT", _DEFAULT_NUM_PREDICT))

_DEFAULT_MIN_PICKS = "6" if any(m in _MODEL_LOWER for m in ["7b", "8b"]) else "8"
MIN_PICKS    = int(os.getenv("MAIA_MIN_PICKS", _DEFAULT_MIN_PICKS))

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

# ─── System prompt (cargado dinámicamente desde agent-prompts.md) ────────────
_AGENT_PROMPTS_PATH = REPO / "references" / "agent-prompts.md"

# Fallback mínimo — solo se usa si el archivo no existe o no tiene la sección
_SYSTEM_PROMPT_FALLBACK = """You are a professional investment analyst for Tododeia, a financial research system.

Your task: analyze the provided market data and produce a complete investment report as a single JSON object.

CRITICAL RULES:
1. Output ONLY valid JSON — no markdown code blocks, no explanation text
2. Include 10-13 investment picks in risk_adjusted_picks
3. Each pick must have ALL required fields with correct data types
4. Use the real price data from MARKET_CONTEXT — do not invent numbers
5. Adapt position sizes and asset mix to the RISK_PROFILE

See the full MegaAgent prompt in references/agent-prompts.md for detailed constraints."""


def _load_mega_agent_prompt() -> str:
    """Extrae el prompt del MegaAgent desde agent-prompts.md.

    Busca la sección '## MegaAgent (Combined Research + Strategy)' y devuelve
    todo su contenido hasta la siguiente sección de nivel 2 o el final del archivo.
    Si el archivo o la sección no existen, devuelve el fallback mínimo.
    """
    if not _AGENT_PROMPTS_PATH.exists():
        print(f"   ⚠️  agent-prompts.md no encontrado en {_AGENT_PROMPTS_PATH} — usando fallback", file=sys.stderr)
        return _SYSTEM_PROMPT_FALLBACK

    try:
        text = _AGENT_PROMPTS_PATH.read_text(encoding="utf-8")
    except Exception as e:
        print(f"   ⚠️  Error leyendo agent-prompts.md: {e} — usando fallback", file=sys.stderr)
        return _SYSTEM_PROMPT_FALLBACK

    # Buscar la sección del MegaAgent: desde "## MegaAgent (Combined..." hasta el próximo "## "
    pattern = r'## MegaAgent \(Combined Research \+ Strategy\)\s*\n(.*?)(?=\n## |\Z)'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        print("   ⚠️  Sección MegaAgent no encontrada en agent-prompts.md — usando fallback", file=sys.stderr)
        return _SYSTEM_PROMPT_FALLBACK

    prompt = match.group(1).strip()
    print(f"   ✅ Prompt cargado desde agent-prompts.md ({len(prompt):,} chars)", file=sys.stderr)
    return prompt


# El prompt se carga una vez al importar el módulo
SYSTEM_PROMPT = _load_mega_agent_prompt()


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

        # Inyectar penalizaciones por símbolo: evitar repetir losers consecutivos
        per_symbol = backtest.get("per_symbol", {})
        if per_symbol:
            penalty_lines = []
            caution_lines = []
            for sym, stats in per_symbol.items():
                consec = stats.get("consecutive_losses", 0)
                hr = stats.get("hit_rate", 1.0)
                sessions = stats.get("sessions", 1)
                avg_score = stats.get("avg_direction_score")
                if consec >= 2:
                    # Hard penalty: 2+ consecutive losses → require strong new catalyst
                    penalty_lines.append(
                        f"  ⚠ {sym}: {consec} consecutive losses "
                        f"(hit_rate={hr*100:.0f}% over {sessions} sessions"
                        + (f", avg_score={avg_score:+.1f}%" if avg_score is not None else "")
                        + ") — only ADD if RSI < 40 AND new fundamental catalyst present; otherwise HOLD or TRIM"
                    )
                elif consec == 1 and sessions >= 2 and hr < 0.4:
                    # Soft caution: losing streak starting + poor overall record
                    caution_lines.append(
                        f"  ~ {sym}: 1 recent loss, overall hit_rate={hr*100:.0f}% — prefer HOLD over ADD"
                    )
            if penalty_lines:
                parts += ["", "=== SYMBOLS WITH REPEATED LOSSES (high bar to ADD) ==="] + penalty_lines
            if caution_lines:
                parts += ["", "=== SYMBOLS WITH CAUTION FLAG ==="] + caution_lines

    # Inyectar picks de la última sesión para enforcer DIVERSITY RULE (regla 7)
    history_dir = REPO / "output" / "history"
    if history_dir.exists():
        history_files = sorted(history_dir.glob("*.json"))
        if history_files:
            try:
                last_report = json.loads(history_files[-1].read_text(encoding="utf-8"))
                last_picks = [
                    p.get("symbol", "").upper().strip()
                    for p in last_report.get("risk_adjusted_picks", [])
                    if p.get("symbol")
                ]
                last_date = history_files[-1].stem  # YYYY-MM-DD from filename
                if last_picks:
                    parts += [
                        "",
                        f"LAST_SESSION_PICKS ({last_date}): {' '.join(last_picks)}",
                        f"→ Max 6 of these {len(last_picks)} symbols may appear again. "
                        "Rotate at least " + str(max(1, len(last_picks) - 6)) + " symbol(s) to fresh candidates from SCREENED_CANDIDATES.",
                    ]
            except Exception:
                pass

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


def call_ollama(context: str, attempt: int, truncated: bool = False) -> str:
    correction = ""
    if attempt > 1:
        if truncated:
            correction = (
                f"\n\n⚠️ ATTEMPT {attempt}/{_MAX_RETRIES}: Previous output was TRUNCATED — "
                f"you stopped before generating all picks. "
                f"You MUST output ALL 10-12 picks in risk_adjusted_picks. "
                "Output ONLY the complete JSON object. Do NOT stop early. Do NOT wrap in markdown."
            )
        else:
            correction = (
                f"\n\n⚠️ ATTEMPT {attempt}/{_MAX_RETRIES}: Previous output was invalid. "
                "You MUST output ONLY a valid JSON object. "
                "Include ALL required fields. Do NOT truncate. Do NOT wrap in markdown."
            )

    user_content = context + correction

    # Qwen3 y deepseek-r1 usan extended thinking por defecto, consumiendo el budget de
    # num_predict antes de generar el JSON. /no_think mantiene el budget para el output.
    is_reasoning = "qwen3" in MODEL.lower() or "deepseek-r1" in MODEL.lower()
    if is_reasoning:
        user_content = "/no_think\n\n" + user_content

    # Usar la API nativa de Ollama (/api/chat) — más estable que el endpoint OpenAI
    payload: dict = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "options": {
            "temperature": 0.2,
            "num_predict": NUM_PREDICT,
            "num_ctx": int(os.getenv("MAIA_NUM_CTX", "8192")),  # reducir a 4096 en modelos 32b con poca VRAM
        },
        "stream": False,
    }
    # Ollama ≥0.6 soporta la bandera top-level "think" para qwen3 / deepseek-r1
    if is_reasoning:
        payload["think"] = False

    resp = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    result = resp.json()
    content = result.get("message", {}).get("content", "")

    # Si el output es SOLO thinking tokens sin JSON, extrae lo que haya tras el último </think>
    if is_reasoning and content.strip():
        m = re.search(r"([\s\S]*)$", content)
        if m and m.group(1).strip():
            content = m.group(1)

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
    # Quitar bloques <think>...</think> de modelos de razonamiento (deepseek-r1, qwen3)
    text = re.sub(r"<\s*think\s*>[\s\S]*?<\s*/\s*think\s*>", "", text)
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

    # Intento 3: truncado — buscar el último pick completo y cerrar el JSON.
    # SOLO activar si risk_adjusted_picks ya empezó; de lo contrario este intento
    # "cura" cross_sector_insights u otras secciones y devuelve JSON sin picks.
    if '"risk_adjusted_picks"' not in text:
        raise json.JSONDecodeError("No se pudo parsear ni reparar el JSON", text, 0)

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

    # Fallback for critical narrative fields the model may omit
    if not data.get("executive_summary"):
        data["executive_summary"] = "No executive summary generated by the strategy model. Please review the picks manually."

    # Fallback for macro_environment — small models often omit this section entirely
    if not data.get("macro_environment"):
        data["macro_environment"] = {
            "summary": "Macro analysis not generated by the strategy model.",
            "interest_rate_outlook": "stable",
            "inflation_outlook": "stable",
            "geopolitical_risk": "medium",
            "key_factors": ["Model output incomplete; using deterministic defaults."],
        }

    # Fallback risk_adjusted_picks — generate from market data when the model
    # returns no picks at all (common for small models).
    if not data.get("risk_adjusted_picks") or len(data.get("risk_adjusted_picks", [])) == 0:
        mkt = load_json_safe(MARKET_CTX, "market_context")
        top_cands = (mkt or {}).get("candidates") or (mkt or {}).get("screened_candidates") or []
        fallback_picks = []
        for idx, cand in enumerate(top_cands[:8]):
            sym = cand.get("symbol", "UNK")
            price = float(cand.get("price") or cand.get("price_at_fetch") or 100.0)
            entry_str = str(cand.get("entry_quality", "")).lower()
            if "excellent" in entry_str:
                conf = 8
            elif "good" in entry_str:
                conf = 7
            elif "fair" in entry_str:
                conf = 6
            else:
                conf = 5
            rsi_val = cand.get("rsi", 50)
            if isinstance(rsi_val, (int, float)):
                risk_score = max(1.0, min(10.0, (float(rsi_val) - 30) / 5.0))
            else:
                risk_score = 5.0
            risk_adj = round(conf * (1 - risk_score / 15.0), 2)
            stop = round(price * 0.92, 2)
            target = round(price * 1.15, 2)
            rr = round((target - price) / (price - stop), 2) if price > stop else 2.0
            fallback_picks.append({
                "rank": idx + 1,
                "name": cand.get("name", sym),
                "symbol": sym,
                "sector": cand.get("sector", "unknown"),
                "confidence": conf,
                "risk_score": round(risk_score, 1),
                "risk_adjusted_score": risk_adj,
                "recommendation": "buy",
                "reasoning": f"Auto-generated fallback — entry quality: {cand.get('entry_quality', '?')}, RSI: {rsi_val}.",
                "position_size": f"{max(2, 10 - idx * 1)}%",
                "entry_price": price,
                "stop_loss": stop,
                "target_12m": target,
                "risk_reward_ratio": rr,
                "thesis": f"Deterministic fallback thesis for {sym}.",
                "thesis_invalidators": ["Price drops below 92% of entry."],
                "thesis_status": "new",
                "financial_health": {
                    "altman_z": None,
                    "altman_zone": "N/A",
                    "piotroski": None,
                    "piotroski_strength": "N/A",
                    "health_note": "Financial health data not available (fallback).",
                },
            })
        if fallback_picks:
            data["risk_adjusted_picks"] = fallback_picks

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


def _print_picks_table(data: dict) -> None:
    """Muestra un resumen legible de los picks en stderr (no interfiere con stdout)."""
    picks = data.get("risk_adjusted_picks", [])
    if not picks:
        print("\n(no picks in report)", file=sys.stderr)
        return

    print("\n--- PICKS SUMMARY (top 8) ---", file=sys.stderr)
    header = f" {'#':<3} {'symbol':<7} {'name':<24} {'conf':>4} {'risk':>4} {'score':>5} {'rec':>8}  thesis (truncated)"
    print(header, file=sys.stderr)
    print("-" * len(header), file=sys.stderr)

    for pick in picks[:8]:
        rank = pick.get("rank", "?")
        sym = pick.get("symbol", "??")
        name = pick.get("name", sym)[:24]
        conf = pick.get("confidence", "?")
        risk = pick.get("risk_score", "?")
        adj = pick.get("risk_adjusted_score", "?")
        rec = pick.get("recommendation", "?")
        thesis = (pick.get("thesis") or "")[:60]
        try:
            rank_s = f"{int(rank):3d}"
        except (ValueError, TypeError):
            rank_s = f"{str(rank):>3}"

        print(
            f" {rank_s} {sym:<7} {name:<24} {conf!s:>4} {risk!s:>4} {adj!s:>5} {rec!s:>8}  {thesis}",
            file=sys.stderr,
        )

    total = len(picks)
    if total > 8:
        print(f"   ... (+{total - 8} more)", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="MegaAgent local — Ollama strategy synthesis")
    parser.add_argument("risk_profile", nargs="?", default="moderate",
                        help="Risk profile: conservative | moderate | aggressive")
    parser.add_argument("--context-file", metavar="FILE",
                        help="Pre-built context file from pipeline.py (skips build_context)")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="Write the generated strategy JSON to FILE (in addition to stdout)")
    parser.add_argument("--assemble", action="store_true",
                        help="After generation, run assemble_report.py and copy the result to the dashboard")
    parser.add_argument("--dashboard", action="store_true",
                        help="Write the generated strategy JSON directly to dashboard/public/data/report.json "
                             "(overwrites the file the UI reads; use for quick iteration without assembly)")
    args = parser.parse_args()
    risk_profile = args.risk_profile

    output_file: Path | None = None
    if args.output:
        output_file = Path(args.output)
        output_file.parent.mkdir(parents=True, exist_ok=True)

    print(f"🤖 MegaAgent local | modelo: {MODEL} | perfil: {risk_profile}", file=sys.stderr)

    if args.context_file:
        ctx_path = Path(args.context_file)
        if not ctx_path.exists():
            print(f"   ❌ context-file no encontrado: {ctx_path}", file=sys.stderr)
            sys.exit(1)
        context = ctx_path.read_text(encoding="utf-8")
        print(f"   Contexto cargado desde {ctx_path} ({len(context):,} chars)", file=sys.stderr)
    else:
        context = build_context(risk_profile)
        print(f"   Contexto construido ({len(context):,} chars)", file=sys.stderr)

    last_error = None
    _truncated = False
    for attempt in range(1, _MAX_RETRIES + 1):
        print(f"   Intento {attempt}/{_MAX_RETRIES}...", file=sys.stderr)
        try:
            raw    = call_ollama(context, attempt, truncated=_truncated)
            data   = extract_json(raw)
            data   = fill_defaults(data, risk_profile)
            errors = validate(data)

            if errors:
                last_error = errors
                print(f"   ⚠️  Validación ({attempt}): {errors[:3]}", file=sys.stderr)
                if attempt < _MAX_RETRIES:
                    continue
                # En el último intento, si hay picks, guardar igual con advertencia
                if data.get("risk_adjusted_picks"):
                    print("   ⚠️  Guardando output parcialmente válido...", file=sys.stderr)
                    _print_picks_table(data)
                    output_json = json.dumps(data, indent=2, ensure_ascii=False)
                    print(output_json)
                    _write_outputs(output_json, output_file, args)
                    sys.exit(0)
                sys.exit(1)

            picks_count = len(data.get("risk_adjusted_picks", []))

            # Detectar truncación: menos picks de los esperados = modelo cortado antes de terminar
            if picks_count < MIN_PICKS and attempt < _MAX_RETRIES:
                print(
                    f"   ⚠️  Truncación detectada ({picks_count} picks < {MIN_PICKS} mínimo) "
                    f"— reintentando (intento {attempt + 1}/{_MAX_RETRIES})...",
                    file=sys.stderr,
                )
                last_error = f"output truncated: only {picks_count} picks"
                _truncated = True
                continue

            print(f"   ✅ JSON válido — {picks_count} picks generados", file=sys.stderr)
            _print_picks_table(data)
            output_json = json.dumps(data, indent=2, ensure_ascii=False)
            print(output_json)
            _write_outputs(output_json, output_file, args)
            return

        except json.JSONDecodeError as e:
            last_error = str(e)
            print(f"   ⚠️  JSON inválido (intento {attempt}): {e}", file=sys.stderr)
            if attempt == _MAX_RETRIES:
                print("   ❌ No se pudo parsear JSON tras todos los reintentos.", file=sys.stderr)
                sys.exit(1)

        except requests.RequestException as e:
            print(f"   ❌ Error conectando a Ollama: {e}", file=sys.stderr)
            print("   Verifica: brew services start ollama  (o: ollama serve)", file=sys.stderr)
            sys.exit(1)


def _write_outputs(output_json: str, output_file: Path | None, args):
    """Persist the strategy JSON to the requested output file and/or dashboard."""
    if output_file:
        output_file.write_text(output_json, encoding="utf-8")
        print(f"   📁 Strategy JSON written to {output_file}", file=sys.stderr)
    if args.dashboard:
        dashboard_file = REPO / "dashboard" / "public" / "data" / "report.json"
        dashboard_file.parent.mkdir(parents=True, exist_ok=True)
        dashboard_file.write_text(output_json, encoding="utf-8")
        print(f"   🖥️  Dashboard updated → {dashboard_file}", file=sys.stderr)
    if args.assemble:
        _auto_assemble(args, output_file)


def _auto_assemble(args, strategy_path: Path | None):
    """Run assemble_report.py and copy latest history to dashboard if possible."""
    if not strategy_path:
        return

    if args.context_file:
        pipeline_dir = Path(args.context_file).parent
    else:
        pipeline_dir = Path("/tmp/tododeia")

    sectors = pipeline_dir / "sectors.json"
    meta    = pipeline_dir / "pipeline_meta.json"

    if not sectors.exists() or not meta.exists():
        missing = []
        if not sectors.exists():
            missing.append(str(sectors))
        if not meta.exists():
            missing.append(str(meta))
        print(f"   ⚠️  No se encontraron los archivos de pipeline: {', '.join(missing)} — assembly skipped", file=sys.stderr)
        return

    assemble_py = REPO / "tools" / "assemble_report.py"
    cmd = [
        sys.executable,
        str(assemble_py),
        "--sectors", str(sectors),
        "--strategy", str(strategy_path),
        "--meta", str(meta),
    ]
    print(f"   🔧 Ejecutando {assemble_py.name} ...", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"   ⚠️  Assembly falló (exit={result.returncode})", file=sys.stderr)
        print(result.stderr[:800], file=sys.stderr)
        return

    # copy latest history to dashboard
    history_dir = REPO / "output" / "history"
    if history_dir.exists():
        history_files = sorted(history_dir.glob("*.json"))
        if history_files:
            latest = history_files[-1]
            dashboard_file = REPO / "dashboard" / "public" / "data" / "report.json"
            dashboard_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(latest), str(dashboard_file))
            print(f"   📊 Dashboard actualizado → {dashboard_file}", file=sys.stderr)
        else:
            print("   ⚠️  No se encontró archivo de historial después del assembly", file=sys.stderr)
    else:
        print("   ⚠️  output/history no existe, no se puede copiar al dashboard", file=sys.stderr)


if __name__ == "__main__":
    main()
