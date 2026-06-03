#!/bin/bash
# run_local.sh — Pipeline completo de Tododeia con Ollama (sin cloud)
# Uso: bash run_local.sh [conservative|moderate|aggressive]
#
# Requiere:
#   - Ollama corriendo con qwen2.5:14b (u otro modelo vía MAIA_MODEL)
#   - pip3 install yfinance pandas numpy requests
#   - Conexión a internet (para yfinance + news + SEC)

set -e

RISK="${1:-moderate}"
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL="${MAIA_MODEL:-qwen2.5:14b}"

echo ""
echo "🚀 Tododeia Local | Perfil: $RISK | Modelo: $MODEL"
echo "────────────────────────────────────────────────"
echo ""

# ── 0. Verificaciones previas ─────────────────────────────────────────────────

# Ollama disponible
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "❌ Ollama no está corriendo."
    echo "   Ejecuta en otra terminal: ollama serve"
    echo "   O como servicio:          brew services start ollama"
    exit 1
fi
echo "✅ Ollama activo"

# Modelo disponible
if ! curl -s http://localhost:11434/api/tags | python3 -c "
import sys, json
models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
target = '$MODEL'
# Verificar con y sin tag :latest
if not any(m == target or m.startswith(target.split(':')[0]) for m in models):
    print(f'Modelo $MODEL no encontrado. Modelos disponibles: {models}')
    exit(1)
" 2>/dev/null; then
    echo "❌ Modelo $MODEL no encontrado."
    echo "   Ejecuta: ollama pull $MODEL"
    exit 1
fi
echo "✅ Modelo $MODEL disponible"
echo ""

cd "$SKILL_DIR"
mkdir -p data output/history dashboard/public/data

# ── 1. Pre-fetch de datos de mercado ─────────────────────────────────────────
echo "📊 Fase 1 — Fetch de datos (internet requerido)..."
echo "  → pre_fetch.py (yfinance, ~30-60s)..."
python3 tools/pre_fetch.py

echo "  → news_fetch.py + sec_risk_fetch.py (paralelo)..."
python3 tools/news_fetch.py &
PID_NEWS=$!
python3 tools/sec_risk_fetch.py &
PID_SEC=$!
wait $PID_NEWS $PID_SEC
echo "  ✅ Datos de mercado descargados"
echo ""

# ── 2. MegaAgent local (Ollama) ───────────────────────────────────────────────
echo "🤖 Fase 2 — MegaAgent (Ollama, ~90-180s)..."
python3 tools/mega_agent.py "$RISK" > /tmp/tododeia_report.json
echo ""

# ── 3. Validar y guardar reporte ─────────────────────────────────────────────
echo "📝 Fase 3 — Guardando reporte..."
python3 tools/write_report.py /tmp/tododeia_report.json
echo ""

# ── 4. Dashboard ─────────────────────────────────────────────────────────────
echo "✅ Pipeline completo."
echo ""
echo "📈 Para ver el dashboard:"
echo "   cd $SKILL_DIR/dashboard && npm run dev -- -p 3420"
echo "   Abre http://localhost:3420"
echo ""
