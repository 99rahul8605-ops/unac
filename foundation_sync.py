import hashlib
import json
import os
import re
from pathlib import Path

import requests

ROOT = Path("/root/unac")
TOPIC_ID = 459
APP_URL = "https://unaclec.vercel.app"
BATCH = "Yoddha Regular Batch for CA Foundation May'26 / Sep'26"

RECEIPTS = ROOT / ".state/txt_uploads"
OUTPUT = ROOT / "exports/foundation_telegram_mapping.json"


def stable_id(value):
    raw = str(value)
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return (slug[:65].strip("-") or "item") + "-" + digest


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "")).casefold().strip()


def read_txt(path):
    batch = ""
    subject = ""
    title = ""
    number = ""
    lectures = []

    for raw in path.read_text(
        encoding="utf-8-sig"
    ).splitlines():
        line = raw.strip()

        if line.upper().startswith("BATCH:"):
            batch = line.partition(":")[2].strip()

        elif line.upper().startswith("SUBJECT:"):
            subject = line.partition(":")[2].strip()

        elif line.upper().startswith("CLASS:"):
            number = line.partition(":")[2].strip()

        elif line.upper().startswith("LECTURE:"):
            title = line.partition(":")[2].strip()

        elif line.upper().startswith("WATCH_URL:"):
            url = line.partition(":")[2].strip()

            if subject and title and url.startswith("https://"):
                lectures.append({
                    "subject": subject,
                    "name": title,
                    "class_number": (
                        int(number) if number.isdigit() else 0
                    ),
                    "watch_url": url,
                })

    return batch, lectures


def find_foundation_txt():
    paths = list((ROOT / "exports").rglob("*.txt"))
    paths += list((ROOT / ".state/txt_uploads").glob("*.txt"))

    matches = []

    for path in set(paths):
        try:
            batch, lectures = read_txt(path)
        except (OSError, UnicodeError):
            continue

        if (
            normalize(batch) == normalize(BATCH)
            and lectures
        ):
            matches.append((len(lectures), path, batch, lectures))

    if not matches:
        return None

    return max(matches, key=lambda item: item[0])


def load_receipt(url):
    # Current forum-aware uploader hashes:
    # SHA256("TOPIC_ID|WATCH_URL")
    digest = hashlib.sha256(
        f"{TOPIC_ID}|{url}".encode()
    ).hexdigest()

    path = RECEIPTS / f"{digest}.json"

    if not path.is_file():
        return {}

    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main():
    result = find_foundation_txt()

    if not result:
        print("❌ Foundation source TXT VPS par nahi mila.")
        print("Original Foundation TXT ko save karo:")
        print("  /root/unac/exports/foundation.txt")
        print("Phir chalao:")
        print("  /root/.venv/bin/python3 foundation_sync.py")
        return

    _, source, batch, lectures = result

    print("✅ Foundation TXT:", source)
    print("Batch:", batch)
    print("Forum Topic:", TOPIC_ID)

    # Do not accidentally map Intermediate or a different batch.
    if normalize(batch) != normalize(BATCH):
        print("⚠️ Batch name expected name se differ karta hai.")
        print("Actual:", batch)
        print("Expected:", BATCH)
        print("Mapping stopped to avoid mixing batches.")
        return

    seen = set()
    mapping = []
    subjects = {}

    for lecture in lectures:
        url = lecture["watch_url"]

        if url in seen:
            continue
        seen.add(url)

        receipt = load_receipt(url)

        # Don't trust name-only matches.
        receipt_name = receipt.get("name", "")
        if (
            receipt_name
            and normalize(receipt_name) != normalize(lecture["name"])
        ):
            print("⚠️ Skipped mismatched receipt:", lecture["name"])
            receipt = {}

        uploaded = receipt.get("uploaded", {})

        def message_id(kind):
            item = uploaded.get(kind) or {}
            value = item.get("message_id")
            return int(value) if value else None

        record = {
            "batch": batch,
            "topic_id": TOPIC_ID,
            "subject": lecture["subject"],
            "class_number": lecture["class_number"],
            "name": lecture["name"],
            "lecture_id": stable_id(url),
            "video_message_id": message_id("video"),
            "board_message_id": message_id("board"),
            "slides_zip_message_id": message_id("slides_zip"),
            "video_uploaded": bool(message_id("video")),
        }

        mapping.append(record)

        subject = lecture["subject"]
        subjects.setdefault(subject, []).append({
            "id": stable_id(url),
            "name": lecture["name"],
            "class_number": lecture["class_number"],
            "duration": 0,
            "stream_url": "",
            "notes_url": ""
        })

    video_count = sum(bool(x["video_message_id"]) for x in mapping)
    board_count = sum(bool(x["board_message_id"]) for x in mapping)
    zip_count = sum(bool(x["slides_zip_message_id"]) for x in mapping)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps({
            "batch": batch,
            "topic_id": TOPIC_ID,
            "lectures": mapping
        }, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print()
    print("===== FOUNDATION MAPPING =====")
    print("Total lectures:", len(mapping))
    print("Videos with saved Telegram IDs:", video_count)
    print("Board JSON with saved IDs:", board_count)
    print("Slides ZIP with saved IDs:", zip_count)
    print("Saved:", OUTPUT)

    for item in mapping:
        if item["video_message_id"]:
            print(
                f"✅ Class {item['class_number']}: "
                f"{item['name'][:40]} "
                f"| video_message_id={item['video_message_id']}"
            )

    # Optional catalog import.
    # This will only run when the admin key is provided.
    key = os.environ.get("ADMIN_IMPORT_KEY", "").strip()

    if not key:
        print()
        print("ℹ️ Mapping ready; Mini App import skipped (key absent).")
        return

    payload = {
        "batches": [{
            "id": stable_id(batch),
            "name": batch,
            "description": "CA Foundation Lectures",
            "published": True,
            "subjects": [
                {
                    "id": stable_id(name),
                    "name": name,
                    "lectures": rows
                }
                for name, rows in subjects.items()
            ]
        }]
    }

    response = requests.post(
        APP_URL + "/api/import",
        headers={"x-admin-import-key": key},
        json=payload,
        timeout=60
    )

    print()
    print("Mini App import HTTP:", response.status_code)
    print(response.text[:800])
    response.raise_for_status()
    print("✅ Foundation catalog imported!")


if __name__ == "__main__":
    main()
