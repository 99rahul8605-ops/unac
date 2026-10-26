#!/usr/bin/env bash
set -Eeuo pipefail

cd /root/unac

PYTHON="/root/.venv/bin/python3"
API_SCREEN="lecture_api"
BOT_SCREEN="unac_bot"
STREAM_SCREEN="foundation_stream"
TUNNEL_SCREEN="bingolink_stream"
TUNNEL_CONFIG="/root/unac/.state/bingolink_stream.yml"
TUNNEL_ID="2cf2c9f7-2137-4d01-b29b-89fe38914c42"

# Usage:
#   bash start.sh        -> restart bot + API; start stream/tunnel only if missing
#   bash start.sh all    -> restart everything (use after changing stream code)
MODE="${1:-}"

# Required files check
for file in bot.py request_api.py foundation_stream.py .env .state/lecture_api.key; do
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

is_running() {
    local out
    out="$(screen -ls 2>/dev/null || true)"
    [[ "$out" == *".$1"* ]]
}

# Stop bot + API (always restarted)
for name in "$API_SCREEN" "$BOT_SCREEN"; do
    screen -S "$name" -X quit 2>/dev/null || true
done

# Stream + tunnel are restarted only in "all" mode
if [[ "$MODE" == "all" ]]; then
    for name in "$STREAM_SCREEN" "$TUNNEL_SCREEN"; do
        screen -S "$name" -X quit 2>/dev/null || true
    done
fi

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

# Start video stream server (port 8092) if not already running
if is_running "$STREAM_SCREEN"; then
    echo "ℹ️ $STREAM_SCREEN already running (use: bash start.sh all)"
else
    screen -dmS "$STREAM_SCREEN" bash -lc '
        cd /root/unac
        exec /root/.venv/bin/python3 -m uvicorn \
            foundation_stream:app \
            --host 127.0.0.1 \
            --port 8092
    '
    echo "✅ started $STREAM_SCREEN"
fi

# Start Cloudflare tunnel for stream.bingolink.site if not already running
if is_running "$TUNNEL_SCREEN"; then
    echo "ℹ️ $TUNNEL_SCREEN already running (use: bash start.sh all)"
elif [[ ! -f "$TUNNEL_CONFIG" ]] || ! command -v cloudflared >/dev/null 2>&1; then
    echo "⚠️ Tunnel not started (missing $TUNNEL_CONFIG or cloudflared)"
else
    screen -dmS "$TUNNEL_SCREEN" bash -c '
        exec cloudflared --config /root/unac/.state/bingolink_stream.yml \
            tunnel run 2cf2c9f7-2137-4d01-b29b-89fe38914c42 \
            >> /root/unac/.state/bingolink_stream.log 2>&1
    '
    echo "✅ started $TUNNEL_SCREEN"
fi

sleep 5

echo ""
echo "🚀 Lecture Manager"
echo "======================"

screen -ls | grep -E 'lecture_api|unac_bot|foundation_stream|bingolink_stream' || true

echo ""
echo "API health (8091):"
curl -fsS --max-time 5 http://127.0.0.1:8091/health || true

echo ""
echo "Stream health (8092):"
curl -fsS --max-time 8 http://127.0.0.1:8092/health | head -c 200 || true

echo ""
echo "Public stream health:"
curl -fsS --max-time 10 https://stream.bingolink.site/health | head -c 200 || true

echo ""
echo ""
echo "✅ Startup attempted."
echo "Restart everything: bash start.sh all"
echo "Screens: screen -r lecture_api | unac_bot | foundation_stream | bingolink_stream"
