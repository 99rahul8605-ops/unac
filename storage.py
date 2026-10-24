import os
import asyncio
import time
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
from pymongo import MongoClient, ASCENDING


class Storage:
    def __init__(self):
        self.group_id = int(os.environ["STORAGE_GROUP_ID"])
        self.bot_token = os.environ["BOT_TOKEN"]

        self.client = MongoClient(
            os.environ["MONGODB_URI"],
            serverSelectionTimeoutMS=12000,
        )

        self.client.admin.command("ping")

        self.db = self.client[
            os.getenv("MONGODB_DB", "lecture_storage")
        ]

        self.db.lecture_topics.create_index(
            [
                ("group_id", ASCENDING),
                ("batch_uid", ASCENDING),
            ],
            unique=True,
        )

        self.db.lecture_files.create_index(
            [
                ("lecture_key", ASCENDING),
                ("filename", ASCENDING),
            ],
            unique=True,
        )

        self.db.lecture_records.create_index(
            "lecture_key", unique=True
        )

    def completed(self, key):
        return bool(self.db.lecture_records.find_one({
            "lecture_key": key,
            "status": "uploaded",
        }))

    async def topic(self, subject):
        existing = self.db.lecture_topics.find_one({
            "group_id": self.group_id,
            "batch_uid": subject["uid"],
        })

        if existing:
            return existing["thread_id"]

        url = (
            f"https://api.telegram.org/bot"
            f"{self.bot_token}/createForumTopic"
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={
                    "chat_id": self.group_id,
                    "name": subject["name"][:128],
                },
            ) as response:
                result = await response.json()

        if not result.get("ok"):
            raise RuntimeError(
                "Topic creation failed: "
                + str(result.get("description", "unknown"))
            )

        thread_id = result["result"]["message_thread_id"]

        self.db.lecture_topics.update_one(
            {
                "group_id": self.group_id,
                "batch_uid": subject["uid"],
            },
            {"$set": {
                "group_id": self.group_id,
                "batch_uid": subject["uid"],
                "thread_id": thread_id,
                "name": subject["name"],
            }},
            upsert=True,
        )

        return thread_id

    async def upload(
        self, telegram, folder, key,
        subject, lecture, on_progress=None,
    ):
        folder = Path(folder)

        video = next(
            (
                p for p in folder.iterdir()
                if p.name in ("video.webm", "video.mp4")
            ),
            None,
        )

        if not video:
            raise RuntimeError("Video missing")

        filenames = [
            "board.json",
            "slides.zip",
            "manifest.json",
            video.name,
        ]

        thread_id = await self.topic(subject)

        for filename in filenames:
            old = self.db.lecture_files.find_one({
                "lecture_key": key,
                "filename": filename,
            })

            if old:
                continue

            path = folder / filename

            if not path.is_file():
                raise RuntimeError(
                    "Missing file: " + filename
                )

            last_update = [0.0]

            def callback(sent, total):
                now = time.monotonic()

                if not on_progress or not total:
                    return

                if (
                    sent != total
                    and now - last_update[0] < 6
                ):
                    return

                last_update[0] = now

                asyncio.create_task(
                    on_progress(
                        filename,
                        sent * 100 / total,
                    )
                )

            message = await telegram.send_file(
                self.group_id,
                str(path),
                caption=(
                    f"Class {lecture['class_number']}\n"
                    f"{lecture['title']}\n{filename}"
                )[:1024],
                reply_to=thread_id,
                force_document=True,
                progress_callback=callback,
            )

            self.db.lecture_files.update_one(
                {
                    "lecture_key": key,
                    "filename": filename,
                },
                {"$set": {
                    "lecture_key": key,
                    "filename": filename,
                    "batch_uid": subject["uid"],
                    "class_number": lecture["class_number"],
                    "group_id": self.group_id,
                    "thread_id": thread_id,
                    "message_id": message.id,
                    "size": path.stat().st_size,
                    "uploaded_at": datetime.now(timezone.utc),
                }},
                upsert=True,
            )

    def mark_complete(
        self, key, goal, pack, subject,
        lecture, manifest,
    ):
        self.db.lecture_records.update_one(
            {"lecture_key": key},
            {"$set": {
                "lecture_key": key,
                "goal_uid": goal["uid"],
                "pack_uid": pack["uid"],
                "batch_uid": subject["uid"],
                "class_number": lecture["class_number"],
                "title": lecture["title"],
                "manifest": manifest,
                "status": "uploaded",
                "updated_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

    def close(self):
        self.client.close()