
"""Create/reuse one Telegram forum topic per batch."""

import asyncio
import json
import re
from pathlib import Path

from telethon.tl.functions.messages import CreateForumTopicRequest

ROOT = Path("/root/unac")
DB = ROOT / ".state" / "batch_forum_topics.json"


def read_batch(path):
    for line in Path(path).read_text(
        encoding="utf-8-sig"
    ).splitlines():
        if line.strip().upper().startswith("BATCH:"):
            name = line.partition(":")[2].strip()
            if name:
                return re.sub(r"\\s+", " ", name)[:128]

    raise RuntimeError(
        "TXT mein BATCH: missing hai. "
        "General topic mein upload nahi kiya jayega."
    )


def load_map():
    if not DB.exists():
        return {}
    return json.loads(DB.read_text(encoding="utf-8"))


def save_map(data):
    DB.parent.mkdir(parents=True, exist_ok=True)
    temp = DB.with_suffix(".tmp")
    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(DB)


async def ensure_batch_topic(client, group_id, txt_path):
    batch = read_batch(txt_path)
    key = f"{group_id}:{batch.casefold()}"

    mapping = load_map()

    if key in mapping:
        return int(mapping[key]), batch

    entity = await client.get_input_entity(group_id)

    try:
        result = await client(
            CreateForumTopicRequest(
                peer=entity,
                title=batch,
            )
        )
    except Exception as exc:
        raise RuntimeError(
            "Batch topic create nahi hua. "
            "Storage group mein Topics ON aur bot ko "
            "Manage Topics permission chahiye. "
            f"Telegram error: {type(exc).__name__}: {exc}"
        ) from exc

    topic_id = None

    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)

        if message is None:
            continue

        action = getattr(message, "action", None)

        if (
            action is not None
            and "TopicCreate" in type(action).__name__
        ):
            topic_id = message.id
            break

    if not topic_id:
        raise RuntimeError(
            "Telegram ne topic create response diya, "
            "lekin topic ID nahi mili. "
            "General mein upload nahi kiya."
        )

    mapping[key] = topic_id
    save_map(mapping)

    return topic_id, batch
