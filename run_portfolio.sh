#!/bin/bash
# run_portfolio.sh — Analiza el portfolio con Ollama (sin cloud)
# Uso: bash run_portfolio.sh
set -e

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL="${MAIA_MODEL:-qwen2.5:14b}"

echo ""
echo "📊 Tododeia — Análisis de Portfolio | Modelo: $MODEL"
echo "──────────────────────────────────────────────────"
echo ""

# Verificar Ollama
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "❌ Ollama no está corriendo."
    echo "   Ejecuta: ollama serve"
    exit 1
fi
echo "✅ Ollama activo"

cd "$SKILL_DIR"

# Verificar portfolio.json
if [ ! -f "data/portfolio.json" ]; then
    echo "❌ data/portfolio.json no encontrado."
    echo "   Crea el archivo con tus posiciones actuales."
    exit 1
fi

POSITIONS=$(python3 -c "import json; p=json.load(open('data/portfolio.json')); print(len(p))" 2>/dev/null || echo "?")
echo "📋 Portfolio: $POSITIONS posiciones"
echo ""

# Fase 1: fetch de datos de mercado del portfolio
echo "📡 Fase 1 — Fetch de datos del portfolio..."
python3 tools/portfolio_fetch.py
echo ""

# Fase 2: análisis con Ollama
echo "🤖 Fase 2 — Análisis con Ollama (~90-150s)..."
python3 tools/portfolio_agent.py > /tmp/portfolio_strategy.json
echo ""

# Fase 3: guardar reporte
echo "📝 Fase 3 — Guardando reporte..."
python3 tools/write_portfolio_report.py /tmp/portfolio_strategy.json
echo ""

echo "✅ Análisis completo."
echo ""
echo "📈 Ver en el dashboard:"
echo "   cd $SKILL_DIR/dashboard && npm run dev -- -p 3420"
echo "   Abre http://localhost:3420 → sección Portfolio"
echo ""
