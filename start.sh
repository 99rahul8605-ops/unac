#!/usr/bin/env bash
set -Eeuo pipefail

cd /root/unac

PYTHON="/root/.venv/bin/python3"
API_SCREEN="lecture_api"
BOT_SCREEN="unac_bot"

# Required files check
for file in bot.py request_api.py .env .state/lecture_api.key; do
    if [[ ! -f "$file" ]]; then
        echo "❌ Missing: $file"
        exit 1
    fi
done

if [[ ! -x "$PYTHON" ]]; then
    echo "❌ Python not found: $PYTHON"
    exit 1
fi

if ! command -v screen >/dev/null 2>&1; then
    echo "❌ GNU Screen not installed"
    exit 1
fi

# Stop ONLY our two screens
for name in "$API_SCREEN" "$BOT_SCREEN"; do
    screen -S "$name" -X quit 2>/dev/null || true
done

sleep 2
screen -wipe >/dev/null 2>&1 || true

mkdir -p logs

# Start Requests API
screen -dmS "$API_SCREEN" bash -c '
    cd /root/unac
    export LECTURE_API_KEY="$(cat .state/lecture_api.key)"
    exec /root/.venv/bin/python3 -m uvicorn \
        request_api:app \
        --host 127.0.0.1 \
        --port 8091
'

# Start Telegram bot
screen -dmS "$BOT_SCREEN" bash -c '
    cd /root/unac
    exec /root/.venv/bin/python3 bot.py
'

sleep 3

echo ""
echo "🚀 Lecture Manager"
echo "======================"

screen -ls | grep -E 'lecture_api|unac_bot' || true

echo ""
echo "API health:"
curl -fsS --max-time 5 http://127.0.0.1:8091/health || true

echo ""
echo ""
echo "✅ Startup attempted."
echo "API screen: screen -r lecture_api"
echo "BOT screen: screen -r unac_bot"
