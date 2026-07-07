# Tododeia — Investment Analysis with Local AI

**by @quebert** · [modCarlos/maia-skill](https://github.com/modCarlos/maia-skill)

Tododeia is a fully local investment analysis pipeline. It fetches real market data for 60+ tickers using Python, runs an AI analyst via **Ollama** (no cloud, no API keys), and produces a risk-adjusted report with investment theses and a Next.js dashboard.

> **[Leer en Español](#español)**

---

## How It Works

```
bash run_local.sh [conservative|moderate|aggressive]
        │
        ▼  Phase 1 — Python tools (no LLM, ~60-90s)
┌────────────────────────────────────────────────────────┐
│  pre_fetch.py        — RSI, trend, fundamentals for    │
│                        60+ tickers via yfinance        │
│  news_fetch.py  ┐                                      │
│  sec_risk_fetch ┘  parallel — headlines + 10-K risks   │
│  backtest.py         — 30/60/90d pick accuracy         │
└────────────────────────┬───────────────────────────────┘
                         │  data/market_context.json
                         │  data/news_context.json
                         │  data/sec_risk_context.json
                         │  data/backtest_summary.json
                         ▼
        ▼  Phase 2 — MegaAgent (Ollama, ~90-180s)
┌────────────────────────────────────────────────────────┐
│  mega_agent.py  — reads all context files, calls       │
│                   local model, outputs structured JSON  │
│  • 10-13 risk-adjusted picks with entry rules          │
│  • Backtesting feedback injected (after 3+ sessions)   │
│  • Fear & Greed synthetic vs alt.me divergence check   │
└────────────────────────┬───────────────────────────────┘
                         │
                         ▼  Phase 3 — Save & Dashboard
                write_report.py → output/history/YYYY-MM-DD.json
                                → dashboard/public/data/report.json
                Next.js dashboard → http://localhost:3420
```

---

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | 3.9+ | |
| pip packages | — | `yfinance pandas numpy requests` |
| Node.js | 18+ | For the dashboard |
| Ollama | latest | [ollama.ai](https://ollama.ai) |
| A local model | — | See [Model Recommendations](#model-recommendations) |
| Internet | — | yfinance + news + SEC EDGAR |

---

## Installation

### macOS / Linux

```bash
# 1. Clone the repo
git clone https://github.com/modCarlos/maia-skill.git
cd maia-skill

# 2. Install Python dependencies
pip3 install yfinance pandas numpy requests

# 3. Install dashboard dependencies
npm install --prefix dashboard

# 4. Install and start Ollama
# Download from https://ollama.ai, then:
ollama pull qwen2.5:14b       # default model (~9 GB)
# or for GPU with 16+ GB VRAM:
ollama pull gemma3:27b        # higher quality (~17 GB)

# 5. Start Ollama (if not running as a service)
ollama serve
```

### Windows (Git Bash required)

1. Install [Git for Windows](https://git-scm.com/download/win) — includes Git Bash
2. Install [Python](https://www.python.org/downloads/) and [Node.js](https://nodejs.org)
3. Install [Ollama for Windows](https://ollama.ai/download/windows)
4. Open **Git Bash** and run:

```bash
git clone https://github.com/modCarlos/maia-skill.git
cd maia-skill
pip install yfinance pandas numpy requests
npm install --prefix dashboard
ollama pull qwen2.5:14b
```

> **Note:** Always use Git Bash (not PowerShell or CMD) to run the `.sh` scripts.

---

## Running the Pipeline

### Daily Market Analysis

```bash
bash run_local.sh                  # moderate risk (default)
bash run_local.sh conservative
bash run_local.sh aggressive
```

The script runs all phases automatically and opens the dashboard at `http://localhost:3420`.

### Portfolio Analysis

First, create your portfolio file:

```bash
cp data/portfolio.json.example data/portfolio.json  # if example exists
# or create data/portfolio.json manually (see format below)
```

Then run:

```bash
bash run_portfolio.sh
```

View results in the dashboard under the **Portfolio** section.

#### `data/portfolio.json` format

```json
[
  {
    "symbol": "NVDA",
    "name": "NVIDIA Corporation",
    "sector": "Technology",
    "quantity": 10,
    "buyPrice": 120.50,
    "buyDate": "2024-11-15"
  }
]
```

---

## Environment Variables

All variables are optional — defaults work out of the box.

| Variable | Default | Description |
|---|---|---|
| `MAIA_MODEL` | `qwen2.5:14b` | Ollama model to use |
| `MAIA_NUM_PREDICT` | `1500` | Max tokens to generate. Set `4000` for fast GPU |
| `MAIA_TIMEOUT` | `480` | Request timeout in seconds. Set `900` for slow models |
| `MAIA_MAX_POSITIONS` | `20` | Max portfolio positions to analyze. Set `30` for GPU with high `num_predict` |

### GPU Usage Example (AMD RX 9070 XT / NVIDIA with 16+ GB VRAM)

```bash
export MAIA_MODEL=gemma3:27b
export MAIA_NUM_PREDICT=4000
bash run_local.sh moderate
```

```bash
# Portfolio with 30 positions
export MAIA_NUM_PREDICT=4000
export MAIA_MAX_POSITIONS=30
bash run_portfolio.sh
```

---

## Model Recommendations

| Hardware | Recommended model | Notes |
|---|---|---|
| Apple M1/M2/M3 (16 GB) | `qwen2.5:14b` | ~5 tok/s — keep `MAIA_NUM_PREDICT=1500` |
| Apple M2/M3 Pro (32+ GB) | `gemma3:27b` | Better quality, ~8 tok/s |
| AMD / NVIDIA GPU (16 GB VRAM) | `gemma3:27b` | 77% GPU / 23% CPU — ~20 tok/s |
| AMD / NVIDIA GPU (24+ GB VRAM) | `qwen2.5:32b` | Full GPU, best quality |

To check what your GPU is using:
```bash
ollama ps
# PROCESSOR column shows CPU/GPU split — aim for > 50% GPU
```

---

## Backtesting

After 3+ daily runs, `backtest.py` automatically evaluates historical picks against current prices and injects performance feedback into the model's prompt.

**How it works:**
- `ADD` picks: "hit" if current price > entry price
- `TRIM` picks: "hit" if current price < entry price
- `HOLD` picks: excluded from hit rate (no directional signal)
- Horizons: 30 / 60 / 90 days
- Segmented by risk profile (conservative / moderate / aggressive)

**Simulating history** (to test before 3 runs):
```bash
mkdir -p output/history
cp dashboard/public/data/report.json output/history/2026-05-04.json  # 30 days ago
cp dashboard/public/data/report.json output/history/2026-04-04.json  # 60 days ago
python3 tools/backtest.py --verbose
```

Results are saved to `data/backtest_summary.json`.

---

## Project Structure

```
maia-skill/
  run_local.sh              # Main pipeline (market analysis)
  run_portfolio.sh          # Portfolio analysis pipeline
  SKILL.md                  # Claude Code skill orchestrator
  tools/
    pre_fetch.py            # yfinance screener — RSI, trend, fundamentals
    news_fetch.py           # Headlines, sentiment, analyst targets
    sec_risk_fetch.py       # SEC EDGAR 10-K risk factor fetcher
    mega_agent.py           # MegaAgent — calls Ollama, builds report
    backtest.py             # Historical pick accuracy evaluator
    portfolio_fetch.py      # yfinance data fetcher for portfolio positions
    portfolio_agent.py      # Portfolio analyst — calls Ollama
    write_report.py         # Schema validator + atomic file writer
    write_portfolio_report.py  # Portfolio report writer
  data/                     # Runtime data (gitignored)
    market_context.json     # pre_fetch output
    news_context.json       # news_fetch output
    sec_risk_context.json   # sec_risk_fetch output
    backtest_summary.json   # backtest output
    portfolio.json          # Your portfolio positions (you create this)
    portfolio_market.json   # portfolio_fetch output
  output/
    history/                # YYYY-MM-DD.json per session (365-day retention)
  dashboard/                # Next.js interactive dashboard
    public/data/
      report.json           # Latest market analysis report
      portfolio_report.json # Latest portfolio analysis report
```

---

## Data Quality Notes

- **RSI**: Uses Wilder's EWM smoothing (`alpha=1/14`) — matches TradingView, Bloomberg, Finviz
- **Analyst targets**: Validated against current price (ratio must be 0.2–5.0); stale/corrupt targets are discarded
- **RSI sanity check**: Values outside 10–98 are rejected (yfinance occasionally returns corrupt data)
- **Fear & Greed**: If alternative.me (crypto-based) diverges > 30 pts from the synthetic (VIX + RSI), the synthetic is used
- **SEC aliases**: GOOGL files under GOOG CIK in SEC EDGAR — handled automatically
- **ETFs excluded**: GLD, SLV, GDX, etc. have no fundamentals in yfinance — removed from all watchlists

---

## Troubleshooting

**Empty response from Ollama:**
The model's context window was exceeded. This was fixed with `num_ctx: 8192` — if it recurs, check `ollama ps` to confirm the model is loaded.

**Read timeout:**
Reduce `MAIA_NUM_PREDICT` or increase `MAIA_TIMEOUT`. For M1 Macs, `MAIA_NUM_PREDICT=1500` is recommended.

**JSON parse error (char 0):**
The model returned empty output — almost always a context window issue. See above.

**`ollama: command not found` on Windows:**
Make sure Ollama is installed and running. Open a new Git Bash window after installation.

**`bash: command not found` on Windows:**
Use Git Bash, not PowerShell or CMD.

**GOOGL no annual filing found:**
Fixed automatically via SEC ticker alias (GOOG). The yfinance fallback finds the 10-K regardless.

---

## Disclaimer

This tool is for **informational and educational purposes only**. It does not constitute financial advice. AI-generated analysis may contain errors. Always consult a qualified financial advisor. Past performance is not indicative of future results.

---

<a id="español"></a>

# Tododeia — Análisis de Inversiones con IA Local

**por @quebert** · [modCarlos/maia-skill](https://github.com/modCarlos/maia-skill)

Pipeline de análisis de inversiones completamente local. Obtiene datos reales de mercado para 60+ tickers con Python, ejecuta un analista IA mediante **Ollama** (sin cloud, sin API keys) y genera un reporte con tesis de inversión y un dashboard en Next.js.

> **[Read in English](#tododeia--investment-analysis-with-local-ai)**

---

## Cómo funciona

El pipeline tiene 3 fases:

**Fase 1 — Python tools (sin LLM, ~60-90s):**
- `pre_fetch.py` — RSI, tendencia, fundamentales para 60+ tickers vía yfinance
- `news_fetch.py` + `sec_risk_fetch.py` — headlines y riesgos 10-K en paralelo
- `backtest.py` — precisión histórica de picks en horizontes 30/60/90 días

**Fase 2 — MegaAgent (Ollama, ~90-180s):**
- Lee todos los archivos de contexto
- Llama al modelo local con contexto comprimido
- Genera 10-13 picks ajustados por riesgo con reglas de entrada explícitas
- Inyecta feedback de backtesting (a partir de la 3ra sesión)

**Fase 3 — Guardar y Dashboard:**
- `write_report.py` guarda el reporte en `output/history/` y actualiza el dashboard
- Dashboard en Next.js disponible en `http://localhost:3420`

---

## Requisitos

- Python 3.9+ con `yfinance pandas numpy requests`
- Node.js 18+ (para el dashboard)
- [Ollama](https://ollama.ai) con un modelo descargado
- Conexión a internet (yfinance + noticias + SEC EDGAR)

---

## Instalación

### macOS / Linux

```bash
git clone https://github.com/modCarlos/maia-skill.git
cd maia-skill
pip3 install yfinance pandas numpy requests
npm install --prefix dashboard

# Instalar Ollama desde https://ollama.ai, luego:
ollama pull qwen2.5:14b       # modelo por defecto (~9 GB)
# o para GPU con 16+ GB VRAM:
ollama pull gemma3:27b        # mayor calidad (~17 GB)

ollama serve   # si no corre como servicio
```

### Windows (requiere Git Bash)

1. Instalar [Git para Windows](https://git-scm.com/download/win) — incluye Git Bash
2. Instalar [Python](https://www.python.org/downloads/) y [Node.js](https://nodejs.org)
3. Instalar [Ollama para Windows](https://ollama.ai/download/windows)
4. Abrir **Git Bash** y ejecutar:

```bash
git clone https://github.com/modCarlos/maia-skill.git
cd maia-skill
pip install yfinance pandas numpy requests
npm install --prefix dashboard
ollama pull qwen2.5:14b
```

> **Importante:** Usar siempre Git Bash (no PowerShell ni CMD) para ejecutar los scripts `.sh`.

---

## Uso

### Análisis de mercado diario

```bash
bash run_local.sh                  # perfil moderado (por defecto)
bash run_local.sh conservative
bash run_local.sh aggressive
```

El script ejecuta todas las fases automáticamente y abre el dashboard en `http://localhost:3420`.

### Análisis de portfolio

Crea `data/portfolio.json` con tus posiciones:

```json
[
  {
    "symbol": "NVDA",
    "name": "NVIDIA Corporation",
    "sector": "Technology",
    "quantity": 10,
    "buyPrice": 120.50,
    "buyDate": "2024-11-15"
  }
]
```

Luego ejecuta:

```bash
bash run_portfolio.sh
```

---

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `MAIA_MODEL` | `qwen2.5:14b` | Modelo de Ollama a usar |
| `MAIA_NUM_PREDICT` | `1500` | Tokens máximos a generar. Usar `4000` en GPU rápida |
| `MAIA_TIMEOUT` | `480` | Timeout en segundos. Usar `900` para modelos lentos |
| `MAIA_MAX_POSITIONS` | `20` | Posiciones máx. a analizar en portfolio. Usar `30` en GPU |

### Ejemplo para GPU (AMD RX 9070 XT / NVIDIA 16+ GB VRAM)

```bash
export MAIA_MODEL=gemma3:27b
export MAIA_NUM_PREDICT=4000
bash run_local.sh moderate
```

```bash
# Portfolio con 30 posiciones
export MAIA_NUM_PREDICT=4000
export MAIA_MAX_POSITIONS=30
bash run_portfolio.sh
```

---

## Modelos recomendados

| Hardware | Modelo recomendado | Notas |
|---|---|---|
| Apple M1/M2 (16 GB) | `qwen2.5:14b` | ~5 tok/s — usar `MAIA_NUM_PREDICT=1500` |
| Apple M2/M3 Pro (32+ GB) | `gemma3:27b` | Mejor calidad, ~8 tok/s |
| AMD / NVIDIA GPU (16 GB VRAM) | `gemma3:27b` | 77% GPU / 23% CPU — ~20 tok/s |
| AMD / NVIDIA GPU (24+ GB VRAM) | `qwen2.5:32b` | GPU completa, mejor calidad |

Para verificar qué está usando tu GPU:
```bash
ollama ps
# La columna PROCESSOR muestra el split CPU/GPU — apuntar a > 50% GPU
```

---

## Backtesting

A partir de la 3ra sesión diaria, `backtest.py` evalúa los picks históricos contra precios actuales e inyecta el feedback en el prompt del modelo.

- Picks **ADD**: acierto si el precio actual > precio de entrada
- Picks **TRIM**: acierto si el precio actual < precio de entrada
- Picks **HOLD**: excluidos del hit rate (sin señal direccional)
- Horizontes: 30 / 60 / 90 días, segmentados por perfil de riesgo

---

## Solución de problemas

**Respuesta vacía de Ollama:** Ventana de contexto excedida. Verificar que se está usando `num_ctx: 8192` (ya incluido en el código).

**Read timeout:** Reducir `MAIA_NUM_PREDICT` o aumentar `MAIA_TIMEOUT`.

**JSON parse error (char 0):** El modelo devolvió output vacío — casi siempre problema de contexto. Ver arriba.

**`bash: command not found` en Windows:** Usar Git Bash, no PowerShell ni CMD.

---

## Aviso legal

Esta herramienta es **solo para fines informativos y educativos**. No constituye asesoramiento financiero. El análisis generado por IA puede contener errores. Siempre consulta a un asesor financiero calificado. El rendimiento pasado no es indicativo de resultados futuros.

---

## Licencia

MIT — ver [LICENSE](LICENSE)
