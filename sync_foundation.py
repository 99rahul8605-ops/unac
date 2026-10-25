import hashlib
import json
import os
import re
from getpass import getpass
from pathlib import Path

import requests

ROOT = Path("/root/unac")
APP_URL = "https://unaclec.vercel.app"


def make_id(value):
    raw = str(value)
    slug = re.sub(
        r"[^a-z0-9]+", "-", raw.lower()
    ).strip("-")
    digest = hashlib.sha256(
        raw.encode()
    ).hexdigest()[:10]
    return (slug[:65].strip("-") or "item") + "-" + digest


def parse_txt(path):
    batch = ""
    subject = ""
    title = ""
    number = ""
    subjects = {}

    for raw in path.read_text(
        encoding="utf-8-sig"
    ).splitlines():
        line = raw.strip()

        if line.upper().startswith("BATCH:"):
            batch = line.split(":", 1)[1].strip()

        elif line.upper().startswith("SUBJECT:"):
            subject = line.split(":", 1)[1].strip()
            subjects.setdefault(subject, [])

        elif line.upper().startswith("CLASS:"):
            number = line.split(":", 1)[1].strip()

        elif line.upper().startswith("LECTURE:"):
            title = line.split(":", 1)[1].strip()

        elif line.upper().startswith("WATCH_URL:"):
            url = line.split(":", 1)[1].strip()

            if subject and title and url.startswith("https://"):
                subjects[subject].append({
                    "name": title,
                    "number": number,
                    "url": url,
                })

    return batch, subjects


def main():
    paths = sorted(
        set(ROOT.glob("exports/**/*.txt"))
        | set(ROOT.glob(".state/txt_uploads/**/*.txt"))
    )

    matches = []

    for path in paths:
        try:
            batch, subjects = parse_txt(path)
        except (OSError, UnicodeError):
            continue

        if "foundation" in batch.casefold():
            count = sum(len(x) for x in subjects.values())
            if count:
                matches.append((path, batch, subjects, count))

    if not matches:
        print("❌ CA Foundation ka source TXT VPS par nahi mila.")
        print("Foundation TXT ko exports/foundation.txt mein rakho.")
        print("Phir yahi command dobara chalao:")
        print("/root/.venv/bin/python3 sync_foundation.py")
        return

    # Select the most complete Foundation TXT.
    path, batch, subjects, count = max(
        matches, key=lambda item: item[3]
    )

    print("✅ Foundation TXT found:", path)
    print("Batch:", batch)
    print("Subjects:", len(subjects))
    print("Lectures:", count)

    converted = []

    for subject_name, lectures in subjects.items():
        unique = {}

        for pos, lecture in enumerate(lectures, 1):
            url = lecture["url"]
            unique[url] = {
                "id": make_id(url),
                "name": lecture["name"],
                "class_number": (
                    int(lecture["number"])
                    if lecture["number"].isdigit()
                    else pos
                ),
                "duration": 0,
                "stream_url": "",
                "notes_url": ""
            }

        converted.append({
            "id": make_id(subject_name),
            "name": subject_name,
            "lectures": list(unique.values())
        })

    payload = {
        "batches": [{
            "id": make_id(batch),
            "name": batch,
            "description": "CA Foundation Lectures",
            "published": True,
            "subjects": converted
        }]
    }

    key = os.environ.get("ADMIN_IMPORT_KEY") or getpass(
        "ADMIN_IMPORT_KEY: "
    )

    response = requests.post(
        APP_URL + "/api/import",
        headers={"x-admin-import-key": key},
        json=payload,
        timeout=60
    )

    print("HTTP:", response.status_code)
    print(response.text[:1000])
    response.raise_for_status()

    print("✅ CA Foundation catalog synced!")
    print("✅ CA Intermediate batch untouched.")


if __name__ == "__main__":
    main()
