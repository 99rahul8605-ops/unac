#!/usr/bin/env bash
set -Eeuo pipefail

cd /root/unac
PY=/root/.venv/bin/python3

if [[ ! -x "$PY" ]]; then
    echo "❌ Python venv missing"
    exit 1
fi

if [[ ! -f txt_uploader.py ]]; then
    echo "❌ txt_uploader.py missing"
    exit 1
fi

# Install uploader dependencies.
"$PY" -m pip install "Pyrogram==2.0.106" tgcrypto

# New Pyrogram-only upload engine.
cat > pyrogram_uploader.py <<'PY'
import asyncio
import os
from pathlib import Path

from dotenv import dotenv_values
from pyrogram import Client, enums

ROOT = Path(__file__).resolve().parent

_app = None
_lock = asyncio.Lock()


def credentials():
    values = dotenv_values(ROOT / ".env")

    def get(name):
        return os.environ.get(name) or values.get(name)

    required = ("API_ID", "API_HASH", "BOT_TOKEN")
    missing = [name for name in required if not get(name)]

    if missing:
        raise RuntimeError(
            "Missing Telegram credentials in .env: "
            + ", ".join(missing)
        )

    return (
        int(get("API_ID")),
        str(get("API_HASH")),
        str(get("BOT_TOKEN")),
    )


async def get_client():
    global _app

    async with _lock:
        if _app is not None:
            return _app

        api_id, api_hash, bot_token = credentials()

        app = Client(
            name="unac_pyrogram_uploader",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            in_memory=True,
            no_updates=True,
            max_concurrent_transmissions=1,
        )

        await app.start()
        _app = app

        return _app


async def send_document(
    destination,
    filename,
    caption,
    progress_callback,
    topic_id,
):
    if not topic_id:
        raise RuntimeError(
            "Batch Forum Topic ID missing. "
            "General mein upload blocked."
        )

    path = Path(filename)

    if not path.is_file():
        raise FileNotFoundError(path)

    if path.stat().st_size == 0:
        raise RuntimeError("Cannot upload empty file")

    app = await get_client()

    def on_progress(current, total):
        if progress_callback:
            progress_callback(current, total)

    result = await app.send_document(
        chat_id=int(destination),
        document=str(path),
        caption=caption,
        parse_mode=enums.ParseMode.DISABLED,
        force_document=True,
        reply_to_message_id=int(topic_id),
        progress=on_progress,
    )

    if result is None:
        raise RuntimeError(
            "Pyrogram upload stopped before completion"
        )

    return result
PY

# Patch only Telegram upload call in existing uploader.
"$PY" - <<'PY'
from pathlib import Path
import re

p = Path("txt_uploader.py")
s = p.read_text()

marker = "PYROGRAM_UPLOAD_ENGINE_V1"

if marker in s:
    print("✅ Pyrogram uploader already connected")
    raise SystemExit(0)

if "ACTIVE_TOPIC_ID" not in s:
    raise SystemExit(
        "❌ Forum topic routing not installed. "
        "Existing files unchanged."
    )

start = s.find("async def upload_one(")

if start < 0:
    raise SystemExit("❌ upload_one function not found")

end = s.find("\nasync def ", start + 1)

if end < 0:
    end = len(s)

section = s[start:end]

pattern = (
    r"(?m)^    message = await client\.send_file\("
    r"\n(?:.*\n)*?^    \)\n"
)

match = re.search(pattern, section)

if not match:
    raise SystemExit(
        "❌ Telethon send_file call not found. "
        "No source files changed."
    )

old = match.group(0)

if (
    "str(path)" not in old
    or "caption=caption" not in old
    or "force_document=True" not in old
):
    raise SystemExit(
        "❌ Upload code structure differs. "
        "No source files changed."
    )

new = '''    # PYROGRAM_UPLOAD_ENGINE_V1
    from pyrogram_uploader import send_document

    message = await send_document(
        destination=destination,
        filename=str(path),
        caption=caption,
        progress_callback=on_progress,
        topic_id=ACTIVE_TOPIC_ID,
    )
'''

section = section[:match.start()] + new + section[match.end():]

updated = s[:start] + section + s[end:]

compile(updated, str(p), "exec")
compile(
    Path("pyrogram_uploader.py").read_text(),
    "pyrogram_uploader.py",
    "exec",
)

backup = Path("txt_uploader.py.before_pyrogram")
backup.write_text(s)
p.write_text(updated)

print("✅ Pyrogram upload engine connected")
print("✅ Forum Topic routing preserved")
print("✅ Resume receipts preserved")
print("✅ Speed callback preserved")
print(f"✅ Backup: {backup}")
PY

"$PY" -m py_compile \
    bot.py txt_uploader.py pyrogram_uploader.py

echo
echo "✅ Pyrogram installation finished"
echo "Restart the bot after current upload completes."
