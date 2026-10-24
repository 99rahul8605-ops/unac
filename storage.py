import os
import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
from pymongo import MongoClient, ASCENDING


class Storage:
    def __init__(self):
        uri = os.environ["MONGODB_URI"]
        name = os.getenv("MONGODB_DB", "lecture_storage")

        self.group_id = int(os.environ["STORAGE_GROUP_ID"])
        self.bot_token = os.environ["BOT_TOKEN"]

        self.mongo = MongoClient(
            uri,
            serverSelectionTimeoutMS=12000,
        )
        self.mongo.admin.command("ping")

        self.db = self.mongo[name]

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
            "lecture_key",
            unique=True,
        )

    def existing_file(self, lecture_key, filename):
        return self.db.lecture_files.find_one({
            "lecture_key": lecture_key,
            "filename": filename,
        })

    def completed(self, lecture_key):
        return self.db.lecture_records.find_one({
            "lecture_key": lecture_key,
            "status": "uploaded",
        }) is not None

    def save_file(
        self,
        lecture_key,
        subject,
        lesson,
        thread_id,
        message_id,
        path,
    ):
        filename = Path(path).name

        self.db.lecture_files.update_one(
            {
                "lecture_key": lecture_key,
                "filename": filename,
            },
            {
                "$set": {
                    "lecture_key": lecture_key,
                    "filename": filename,
                    "group_id": self.group_id,
                    "thread_id": thread_id,
                    "message_id": message_id,
                    "batch_uid": subject["uid"],
                    "class_number": lesson["class_number"],
                    "size": Path(path).stat().st_size,
                    "uploaded_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    def mark_complete(
        self,
        lecture_key,
        goal,
        pack,
        subject,
        lesson,
        manifest,
    ):
        self.db.lecture_records.update_one(
            {"lecture_key": lecture_key},
            {
                "$set": {
                    "lecture_key": lecture_key,
                    "goal_uid": goal["uid"],
                    "pack_uid": pack["uid"],
                    "batch_uid": subject["uid"],
                    "class_number": lesson["class_number"],
                    "title": lesson["title"],
                    "manifest": manifest,
                    "status": "uploaded",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
            upsert=True,
        )

    async def get_topic(self, subject):
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
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                data = await response.json()

        if not data.get("ok"):
            raise RuntimeError(
                "Telegram topic creation failed: "
                + str(data.get("description", "unknown error"))
            )

        thread_id = data["result"]["message_thread_id"]

        self.db.lecture_topics.update_one(
            {
                "group_id": self.group_id,
                "batch_uid": subject["uid"],
            },
            {
                "$set": {
                    "group_id": self.group_id,
                    "batch_uid": subject["uid"],
                    "name": subject["name"],
                    "thread_id": thread_id,
                }
            },
            upsert=True,
        )

        return thread_id

    async def upload_package(
        self,
        client,
        folder,
        lecture_key,
        subject,
        lesson,
        progress=None,
    ):
        folder = Path(folder)
        thread_id = await self.get_topic(subject)

        video = next(
            (
                p for p in folder.iterdir()
                if p.name in ("video.webm", "video.mp4")
            ),
            None,
        )

        if video is None:
            raise RuntimeError("Video file missing")

        files = [
            folder / "board.json",
            folder / "manifest.json",
            folder / "slides.zip",
            video,
        ]

        for file in files:
            if not file.is_file():
                raise RuntimeError(
                    f"Package file missing: {file.name}"
                )

            if self.existing_file(
                lecture_key,
                file.name,
            ):
                continue

            last_update = [0]

            def callback(sent, total):
                if not progress or not total:
                    return

                current = time.monotonic()

                if (
                    current - last_update[0] < 6
                    and sent != total
                ):
                    return

                last_update[0] = current

                asyncio.create_task(
                    progress(
                        file.name,
                        sent * 100 / total,
                    )
                )

            message = await client.send_file(
                self.group_id,
                str(file),
                caption=(
                    f"Class {lesson['class_number']}\n"
                    f"{lesson['title']}\n"
                    f"{file.name}"
                )[:1024],
                reply_to=thread_id,
                force_document=True,
                progress_callback=callback,
            )

            self.save_file(
                lecture_key,
                subject,
                lesson,
                thread_id,
                message.id,
                file,
            )

    def close(self):
        self.mongo.close()
