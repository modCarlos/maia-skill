#!/bin/bash
# run_local.sh — Pipeline completo de Tododeia v2 con Ollama (sin cloud)
# Uso: bash run_local.sh [conservative|moderate|aggressive]
#
# Requiere:
#   - Ollama corriendo con maia-agent (o MAIA_MODEL=qwen3:14b)
#   - pip3 install yfinance pandas numpy requests
#   - Conexión a internet (para yfinance + news + SEC)
#   - Para crear maia-agent: ollama create maia-agent -f Modelfile.qwen3
#     (requiere OLLAMA_FLASH_ATTENTION=1 y OLLAMA_KV_CACHE_TYPE=q8_0)

set -e

RISK="${1:-moderate}"
SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
MODEL="${MAIA_MODEL:-maia-agent}"
# Resolve temp dir cross-platform: Git Bash maps /tmp on Windows; Python uses %TEMP%
OUT_DIR="${TODODEIA_OUT_DIR:-$(python3 -c 'import tempfile,os; print(os.path.join(tempfile.gettempdir(),"tododeia"))')}"
DASHBOARD_PORT="${DASHBOARD_PORT:-3420}"
export MAIA_NUM_PREDICT="${MAIA_NUM_PREDICT:-4000}"
# Forzar UTF-8 en todos los subprocesos Python (Windows cp1252 no soporta emojis)
export PYTHONUTF8=1

echo ""
echo "🚀 Tododeia v2 | Perfil: $RISK | Modelo: $MODEL"
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

# Modelo disponible — si maia-agent no existe, sugerir qwen3:14b como fallback
if ! curl -s http://localhost:11434/api/tags | python3 -c "
import sys, json
models = [m['name'] for m in json.load(sys.stdin).get('models', [])]
target = '$MODEL'
if not any(m == target or m.startswith(target.split(':')[0]) for m in models):
    print(f'Modelo {target} no encontrado.')
    if target == 'maia-agent':
        print('  → Para crearlo: cd $(dirname \$0) && ollama create maia-agent -f Modelfile.qwen3')
        print('  → Alternativa:  MAIA_MODEL=qwen3:14b bash run_local.sh $RISK')
    else:
        print(f'  → Instálalo con: ollama pull {target}')
    exit(1)
" 2>/dev/null; then
    exit 1
fi
echo "✅ Modelo $MODEL disponible"
echo ""

cd "$SKILL_DIR"
mkdir -p "$OUT_DIR"

# ── 1. Pipeline determinístico ────────────────────────────────────────────────
# Ejecuta en orden: pre_fetch → news/sec (paralelo) → accuracy →
# build_sectors → update_stops → compress_context → pipeline_meta.json

echo "📊 Fase 1 — Pipeline determinístico..."
echo "  (pre_fetch + news + SEC + accuracy + sectors + compress)"
python3 tools/pipeline.py --risk-profile "$RISK" --watchlist all --out-dir "$OUT_DIR"
echo "  ✅ Pipeline completo → $OUT_DIR"
echo ""

# ── 2. MegaAgent local (Ollama) ───────────────────────────────────────────────
# Lee el contexto pre-comprimido — no necesita re-fetchar datos

echo "🤖 Fase 2 — MegaAgent estrategia (Ollama, ~90-180s)..."
python3 tools/mega_agent.py "$RISK" --context-file "$OUT_DIR/mega_context.txt" \
    > "$OUT_DIR/strategy.json"
echo "  ✅ Estrategia generada → $OUT_DIR/strategy.json"
echo ""

# ── 3. Ensamblar reporte ──────────────────────────────────────────────────────
# Normaliza picks, recomputa scores, aplica límites de correlación,
# escribe history + dashboard JSON vía write_report.py

echo "📝 Fase 3 — Ensamblando reporte final..."
python3 tools/assemble_report.py \
    --sectors  "$OUT_DIR/sectors.json" \
    --strategy "$OUT_DIR/strategy.json" \
    --meta     "$OUT_DIR/pipeline_meta.json"
echo "  ✅ Reporte ensamblado"
echo ""

# ── 4. Servir dashboard ───────────────────────────────────────────────────────

echo "✅ Pipeline completo."
echo ""

if curl -s "http://localhost:$DASHBOARD_PORT" > /dev/null 2>&1; then
    echo "📈 Dashboard ya activo → http://localhost:$DASHBOARD_PORT"
    echo "   (recarga la página para ver los nuevos datos)"
else
    echo "🌐 Iniciando dashboard en http://localhost:$DASHBOARD_PORT ..."
    python3 tools/serve_report.py --port "$DASHBOARD_PORT" &
    DASHBOARD_PID=$!
    echo "   PID: $DASHBOARD_PID (Ctrl+C para detener)"
    echo ""
    for i in $(seq 1 15); do
        sleep 1
        if curl -s "http://localhost:$DASHBOARD_PORT" > /dev/null 2>&1; then
            echo "   ✅ Dashboard listo → http://localhost:$DASHBOARD_PORT"
            break
        fi
    done
fi
echo ""
