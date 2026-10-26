#!/usr/bin/env bash
# Uploader-only start (bot + lecture_api). No stream server, no tunnel.
set -Eo pipefail

cd /root/unac
PYTHON="/root/.venv/bin/python3"

for file in bot.py request_api.py .env; do
    [[ -f "$file" ]] || { echo "ERROR: Missing: $file"; exit 1; }
done
[[ -x "$PYTHON" ]] || { echo "ERROR: Python nahi mila: $PYTHON"; exit 1; }
command -v screen >/dev/null 2>&1 || { echo "ERROR: screen install nahi hai"; exit 1; }

for key in API_ID API_HASH BOT_TOKEN OWNER_ID STORAGE_GROUP_ID MONGODB_URI; do
    grep -q "^${key}=." .env || { echo "ERROR: .env me ${key} khaali ya missing hai"; exit 1; }
done

mkdir -p .state logs

if [[ ! -s .state/lecture_api.key ]]; then
    "$PYTHON" -c "import secrets; print(secrets.token_hex(32))" > .state/lecture_api.key
    chmod 600 .state/lecture_api.key
    echo "INFO: naya lecture_api.key bana diya"
fi

# Stop everything of ours first so only ONE bot ever runs
screen -S lecture_api -X quit 2>/dev/null || true
screen -S unac_bot -X quit 2>/dev/null || true
pkill -f "python3 bot.py" 2>/dev/null || true
pkill -f "request_api:app" 2>/dev/null || true
sleep 3
screen -wipe >/dev/null 2>&1 || true

screen -dmS lecture_api bash -c '
    cd /root/unac
    export LECTURE_API_KEY="$(cat .state/lecture_api.key)"
    exec /root/.venv/bin/python3 -m uvicorn request_api:app \
        --host 127.0.0.1 --port 8091 >> logs/lecture_api.log 2>&1
'

screen -dmS unac_bot bash -c '
    cd /root/unac
    exec /root/.venv/bin/python3 bot.py >> logs/bot.log 2>&1
'

sleep 8

echo ""
echo "===== STATUS ====="
screen -ls | grep -E "lecture_api|unac_bot" || echo "ERROR: koi screen nahi chala"

BOTS="$(pgrep -fc '^/root/.venv/bin/python3 bot.py$' || true)"
echo "bot.py processes: ${BOTS} (1 hona chahiye)"

CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8091/health || true)"
echo "lecture_api HTTP: ${CODE} (000 = band hai)"

if [[ ! -d "$HOME/.cache/ms-playwright" ]]; then
    echo "WARNING: Playwright Chromium install nahi hai:"
    echo "   /root/.venv/bin/python3 -m playwright install chromium"
    echo "   /root/.venv/bin/python3 -m playwright install-deps chromium"
fi

if [[ "$CODE" == "000" ]]; then
    echo ""
    echo "--- lecture_api.log (aakhri 12 lines) ---"
    tail -12 logs/lecture_api.log 2>/dev/null || true
fi

if [[ "$BOTS" != "1" ]]; then
    echo ""
    echo "--- bot.log (aakhri 12 lines) ---"
    tail -12 logs/bot.log 2>/dev/null || true
fi
