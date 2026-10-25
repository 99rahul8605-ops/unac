import json
import re
from pathlib import Path

src = Path(
    "/root/unac/exports/"
    "Yoddha_Regular_Batch_for_CA_Foundation_May_26_Sep_26_20261025_150013.txt"
)

dst = Path("/root/unac/exports/yoddha_foundation_may26_sep26_import.json")

text = src.read_text(encoding="utf-8")

batch_match = re.search(r"^BATCH:\s*(.+)$", text, re.M)
if not batch_match:
    raise SystemExit("ERROR: Batch name missing")

sections = re.split(r"(?=^SUBJECT:\s*)", text, flags=re.M)

subjects = []

for section in sections:
    match = re.search(r"^SUBJECT:\s*(.+)$", section, re.M)
    if not match:
        continue

    subject_name = match.group(1).strip()
    subject_index = len(subjects) + 1

    lectures = []
    current = None

    def save_lecture():
        if not current:
            return
        url = current.get("url", "")
        name = current.get("name", "")

        if not url.startswith("https://") or not name:
            return
        if len(url) > 2048:
            raise ValueError("Watch URL exceeds 2048 characters")

        index = len(lectures) + 1

        lectures.append({
            "id": f"yfm26_s{subject_index:02d}_l{index:04d}",
            "name": name[:200],
            "class_number": current.get("number", index),
            "duration": 0,
            "stream_url": url,
            "notes_url": ""
        })

    for line in section.splitlines():
        line = line.strip()

        if line.startswith("CLASS:"):
            save_lecture()
            current = {"number": int(line.split(":", 1)[1].strip())}

        elif line.startswith("LECTURE:") and current is not None:
            current["name"] = line.split(":", 1)[1].strip()

        elif line.startswith("WATCH_URL:") and current is not None:
            current["url"] = line.split(":", 1)[1].strip()

    save_lecture()

    subjects.append({
        "id": f"yfm26_subject_{subject_index:02d}",
        "name": subject_name[:200],
        "lectures": lectures
    })

total = sum(len(s["lectures"]) for s in subjects)

if len(subjects) != 9 or total != 395:
    raise SystemExit(
        f"ERROR: Expected 9 subjects / 395 lectures, got "
        f"{len(subjects)} subjects / {total} lectures. Import stopped."
    )

batch = {
    "id": "yoddha_ca_foundation_may26_sep26",
    "name": batch_match.group(1).strip(),
    "description": "CA Foundation May 2026 / September 2026",
    "published": True,
    "subjects": subjects
}

dst.write_text(
    json.dumps({"batches": [batch]}, ensure_ascii=False, indent=2),
    encoding="utf-8"
)

print("✅ JSON CREATED")
print("Batch:", batch["name"])
print("Subjects:", len(subjects))
print("Lectures:", total)
print("File:", dst)

for subject in subjects:
    print(f'  {subject["name"]}: {len(subject["lectures"])} lectures')
