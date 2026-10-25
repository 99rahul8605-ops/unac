from pathlib import Path
import ast
import shutil
from datetime import datetime

root = Path("/root/unac")
engine = root / "pyrogram_uploader.py"
uploader = root / "txt_uploader.py"

if not engine.is_file() or not uploader.is_file():
    raise SystemExit("Required uploader files missing; no changes made")

old_engine = engine.read_text()
old_uploader = uploader.read_text()

if "PYROGRAM_PEER_BRIDGE_V2" in old_engine:
    raise SystemExit("Already installed")

# Replace only the Pyrogram upload engine.
# Do not change the download, receipts or forum routing.
new_engine = '''
"""Pyrogram upload engine with Telethon peer bridge."""

import asyncio
import os
from pathlib import Path

from dotenv import dotenv_values
from pyrogram import Client, enums

# PYROGRAM_PEER_BRIDGE_V2

ROOT = Path(__file__).resolve().parent
_app = None
_lock = asyncio.Lock()
_ready_peers = set()


def credentials():
    values = dotenv_values(ROOT / ".env")

    def get(name):
        return os.environ.get(name) or values.get(name)

    missing = [
        name for name in ("API_ID", "API_HASH", "BOT_TOKEN")
        if not get(name)
    ]
    if missing:
        raise RuntimeError(
            "Missing .env credentials: " + ", ".join(missing)
        )

    return int(get("API_ID")), str(get("API_HASH")), str(get("BOT_TOKEN"))


async def get_client():
    global _app

    async with _lock:
        if _app is not None:
            return _app

        api_id, api_hash, token = credentials()

        app = Client(
            "unac_pyrogram_uploader",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            in_memory=True,
            no_updates=True,
            max_concurrent_transmissions=1,
        )

        await app.start()
        _app = app
        return app


async def ensure_peer(app, telethon_client, destination):
    group_id = int(destination)

    if group_id in _ready_peers:
        return

    from telethon.tl.types import InputPeerChannel, InputPeerChat

    # Do not accidentally reuse an access hash from a
    # different Telegram account.
    telethon_me = await telethon_client.get_me()
    pyrogram_me = await app.get_me()

    if int(telethon_me.id) != int(pyrogram_me.id):
        raise RuntimeError(
            "Telethon/Pyrogram use different Telegram accounts. "
            "Peer cache cannot be shared."
        )

    peer = await telethon_client.get_input_entity(group_id)

    if isinstance(peer, InputPeerChannel):
        if not peer.access_hash:
            raise RuntimeError(
                "Telethon has no access hash for storage group"
            )

        # Pyrogram uses full -100... ID in peer storage.
        await app.storage.update_peers([
            (
                group_id,
                int(peer.access_hash),
                "supergroup",
                None,
                None,
            )
        ])

    elif isinstance(peer, InputPeerChat):
        await app.storage.update_peers([
            (group_id, 0, "group", None, None)
        ])

    else:
        raise RuntimeError(
            "Storage destination is not a Telegram group"
        )

    # Verify Pyrogram can resolve the destination
    # before attempting any file upload.
    await app.resolve_peer(group_id)

    _ready_peers.add(group_id)


async def send_document(
    destination,
    filename,
    caption,
    progress_callback,
    topic_id,
    source_client,
):
    if not topic_id:
        raise RuntimeError(
            "Batch topic missing; General upload blocked"
        )

    path = Path(filename)

    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Upload file missing or empty")

    app = await get_client()

    await ensure_peer(
        app,
        source_client,
        int(destination),
    )

    def progress(current, total):
        if progress_callback:
            progress_callback(current, total)

    return await app.send_document(
        chat_id=int(destination),
        document=str(path),
        caption=caption,
        parse_mode=enums.ParseMode.DISABLED,
        force_document=True,
        reply_to_message_id=int(topic_id),
        progress=progress,
    )
'''

# Locate send_document call inside upload_one.
tree = ast.parse(old_uploader)
fn = next(
    (
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "upload_one"
    ),
    None,
)

if fn is None:
    raise SystemExit("upload_one not found; no changes made")

calls = [
    node for node in ast.walk(fn)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "send_document"
]

if len(calls) != 1:
    raise SystemExit(
        "Expected one Pyrogram send_document call; no changes made"
    )

call = calls[0]
if any(k.arg == "source_client" for k in call.keywords):
    raise SystemExit("Peer bridge argument already present")

lines = old_uploader.splitlines(keepends=True)

# Insert new argument before call's closing parenthesis.
# Existing send_document call uses a multiline layout.
closing_line = lines[call.end_lineno - 1]

if closing_line.strip() != ")":
    raise SystemExit(
        "Unexpected send_document formatting; no changes made"
    )

indent = " " * (call.col_offset + 4)
lines.insert(
    call.end_lineno - 1,
    indent + "source_client=client,\n",
)

new_uploader = "".join(lines)

# Validate both files before writing.
compile(new_engine, str(engine), "exec")
compile(new_uploader, str(uploader), "exec")

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

shutil.copy2(
    engine,
    root / f"pyrogram_uploader.py.before_peer_bridge_{stamp}"
)
shutil.copy2(
    uploader,
    root / f"txt_uploader.py.before_peer_bridge_{stamp}"
)

engine.write_text(new_engine)
uploader.write_text(new_uploader)

print("✅ Pyrogram peer bridge installed")
print("✅ Video, board and assets still use Pyrogram")
print("✅ Existing receipts preserved")
print("✅ Forum topic routing preserved")
print("✅ Backups created")
