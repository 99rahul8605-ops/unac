
import os
import json
import time
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

ROOT = Path("/root/unac")
FOLDER = ROOT / "api_downloads/2674c8e7fdf2"
RECEIPT = FOLDER / "telegram_upload_receipt.json"

load_dotenv(ROOT / ".env")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
GROUP_ID = int(os.environ["STORAGE_GROUP_ID"])

FILES = [
    ("Video", "1_video.webm"),
    ("Board JSON", "2_board.json"),
    ("Slide", "3_asset_1.jpg"),
]


async def main():
    completed = {}

    if RECEIPT.exists():
        completed = json.loads(
            RECEIPT.read_text(encoding="utf-8")
        )

    client = TelegramClient(
        str(ROOT / ".state/upload_once_session"),
        API_ID,
        API_HASH,
    )

    await client.start(bot_token=BOT_TOKEN)

    try:
        group = await client.get_input_entity(GROUP_ID)

        for label, name in FILES:
            path = FOLDER / name

            if name in completed:
                print(f"SKIPPED: {name}", flush=True)
                continue

            if not path.is_file():
                print(f"MISSING: {path}", flush=True)
                continue

            print(f"UPLOADING: {name}", flush=True)

            last_print = [0.0]

            def progress(current, total):
                now = time.monotonic()

                if now - last_print[0] >= 4 or current == total:
                    percent = (
                        current * 100 / total if total else 0
                    )
                    print(
                        f"{label}: {percent:.1f}%",
                        flush=True,
                    )
                    last_print[0] = now

            message = await client.send_file(
                group,
                str(path),
                caption=(
                    "General Clauses act - sections 21 to 24\n"
                    f"Resource: {label}"
                ),
                force_document=(label != "Video"),
                supports_streaming=(label == "Video"),
                progress_callback=progress,
            )

            completed[name] = {
                "message_id": message.id,
                "size": path.stat().st_size,
            }

            temporary = RECEIPT.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(completed, indent=2),
                encoding="utf-8",
            )
            temporary.replace(RECEIPT)

            print(f"UPLOADED: {name}", flush=True)

        print("UPLOAD PROCESS FINISHED", flush=True)

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
