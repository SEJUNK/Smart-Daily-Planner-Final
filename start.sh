#!/usr/bin/env bash
# ============================================================
#  Smart Daily Planner — Normal Mode Launcher (Linux / Mac)
#
#  Prerequisites: complete PRE_TESTING_GUIDE.md first.
#  Requires .env file with real API keys.
#
#  Usage:
#    chmod +x start.sh
#    ./start.sh
# ============================================================

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo ""
echo " ============================================"
echo "  Smart Daily Planner — Normal Mode"
echo " ============================================"
echo ""

# ── Load .env ─────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    echo " [ERROR] .env file not found."
    echo " Copy .env.example → .env and fill in your API keys."
    exit 1
fi
set -a; source .env; set +a

# ── Activate venv if present ──────────────────────────────────
if [ -d ".venv" ]; then
    source .venv/bin/activate
    echo " [+] Virtual environment activated."
fi

# ── Create __init__.py markers ────────────────────────────────
for pkg in config api agents tools mcp_servers; do
    [ -f "$pkg/__init__.py" ] || touch "$pkg/__init__.py"
done

# ── Start server ──────────────────────────────────────────────
PORT="${PORT:-5000}"
echo " [+] Starting at http://localhost:${PORT}"
echo " [+] API docs: http://localhost:${PORT}/docs"
echo " Press Ctrl+C to stop."
echo ""

python -m uvicorn api.main:app \
    --host 127.0.0.1 \
    --port "${PORT}" \
    --reload \
    --reload-dir api \
    --reload-dir agents \
    --reload-dir tools \
    --reload-dir config \
    --log-level info
