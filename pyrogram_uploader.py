
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

        (ROOT / ".state").mkdir(parents=True, exist_ok=True)

        app = Client(
            "unac_pyrogram_uploader",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=token,
            in_memory=False,
            workdir=str(ROOT / ".state"),
            # PYROGRAM_FLOOD_FIX_V1
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
