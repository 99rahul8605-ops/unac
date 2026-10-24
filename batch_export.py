
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from catalog import classes

ROOT = Path("/root/unac")
STATE = ROOT / ".state"
DB = STATE / "jobs.sqlite3"
OUTPUT = ROOT / "exports"
PROFILE = STATE / "verified_backup"

OUTPUT.mkdir(parents=True, exist_ok=True)


def get_job(job_id):
    if not DB.exists():
        raise RuntimeError("jobs.sqlite3 not found")

    connection = sqlite3.connect(str(DB))

    try:
        row = connection.execute(
            "SELECT payload FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()

    if not row:
        raise RuntimeError(f"Job #{job_id} not found")

    data = json.loads(row[0])

    if not isinstance(data, dict):
        raise RuntimeError("Invalid job payload")

    return data


def get_subjects(payload):
    subjects = payload.get("subjects")

    if subjects is None:
        subjects = payload.get("selected_subjects")

    if isinstance(subjects, dict):
        subjects = list(subjects.values())

    if not isinstance(subjects, list):
        raise RuntimeError(
            "Subjects not found in job payload. "
            f"Available keys: {list(payload.keys())}"
        )

    result = []

    for item in subjects:
        if isinstance(item, dict):
            result.append(item)

    if not result:
        raise RuntimeError(
            "No subject objects found in job payload"
        )

    return result


def pick(data, *keys):
    for key in keys:
        value = data.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


async def export_job(job_id):
    payload = get_job(job_id)
    subjects = get_subjects(payload)

    batch = payload.get("pack") or {}
    goal = payload.get("goal") or {}

    if not isinstance(batch, dict):
        batch = {}

    if not isinstance(goal, dict):
        goal = {}

    manifest = {
        "format_version": 1,
        "job_id": job_id,
        "goal": pick(goal, "name", "title"),
        "batch": pick(batch, "name", "title"),
        "subjects": [],
    }

    lines = [
        f"JOB: {job_id}",
        f"GOAL: {manifest['goal']}",
        f"BATCH: {manifest['batch']}",
        "",
    ]

    for index, subject in enumerate(subjects, 1):
        subject_name = pick(
            subject, "name", "title", "subject"
        ) or f"Subject {index}"

        print(
            f"[{index}/{len(subjects)}] "
            f"Discovering: {subject_name}",
            flush=True,
        )

        try:
            lectures, total = await classes(
                PROFILE,
                subject,
            )

        except Exception as error:
            print(f"  ERROR: {error}", flush=True)

            manifest["subjects"].append({
                "name": subject_name,
                "error": str(error),
                "lectures": [],
            })

            lines.extend([
                "=" * 55,
                f"SUBJECT: {subject_name}",
                f"ERROR: {error}",
                "",
            ])
            continue

        record = {
            "name": subject_name,
            "total_classes": total,
            "lectures": [],
        }

        lines.extend([
            "=" * 55,
            f"SUBJECT: {subject_name}",
            f"TOTAL CLASSES: {total}",
            f"WATCH LINKS: {len(lectures)}",
            "",
        ])

        for position, lecture in enumerate(lectures, 1):
            number = lecture.get(
                "class_number", position
            )

            title = pick(
                lecture,
                "title",
                "name",
                "lecture",
            ) or f"Lecture {number}"

            watch_url = pick(
                lecture,
                "url",
                "watch_url",
                "link",
            )

            item = {
                "class_number": number,
                "name": title,
                "watch_url": watch_url,
                "video_url": None,
                "board_json_url": None,
                "slides": [],
                "notes": [],
                "resolution_status": "pending",
            }

            record["lectures"].append(item)

            lines.extend([
                f"CLASS: {number}",
                f"LECTURE: {title}",
                f"WATCH_URL: {watch_url or 'NOT_FOUND'}",
                "VIDEO_URL: NOT_RESOLVED",
                "BOARD_JSON_URL: NOT_RESOLVED",
                "SLIDES: NOT_RESOLVED",
                "NOTES: NOT_RESOLVED",
                "",
            ])

        manifest["subjects"].append(record)

        print(
            f"  Found: {len(lectures)}/{total}",
            flush=True,
        )

    text_path = OUTPUT / f"job_{job_id}_lectures.txt"
    json_path = OUTPUT / f"job_{job_id}_lectures.json"

    text_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )

    json_path.write_text(
        json.dumps(
            manifest,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print("\nExport completed")
    print("TXT :", text_path)
    print("JSON:", json_path)


if __name__ == "__main__":
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit(
            "Usage: python3 batch_export.py JOB_ID"
        )

    asyncio.run(export_job(int(sys.argv[1])))
