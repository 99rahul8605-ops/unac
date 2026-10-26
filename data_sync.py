"""Export / import of uploader data between servers.

Exports receipts, the batch->topic map and TXT files into one ZIP.
Import MERGES (never overwrites) and is deterministic, so exporting from A,
importing on B and the other way round converges to the same data, with no
duplicates. Secrets (.env, sessions, keys, browser profile) are never exported.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

FORMAT = "unac-sync-v1"

RECEIPT_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
INPUT_NAME = re.compile(r"^input_[A-Za-z0-9_.-]{1,100}\.txt$")
EXPORT_NAME = re.compile(r"^[A-Za-z0-9_.' ()\-]{1,160}\.txt$")
TOPIC_KEY = re.compile(r"^-?\d+:.+$")

MAX_ZIP = 100 * 1024 * 1024
MAX_TOTAL = 300 * 1024 * 1024
MAX_MEMBER = 5 * 1024 * 1024
MAX_FILES = 60000
KEEP_BACKUPS = 5

ROOT = STATE = WORK = EXPORTS = TOPICS = BACKUPS = None


def configure(root):
    global ROOT, STATE, WORK, EXPORTS, TOPICS, BACKUPS
    ROOT = Path(root)
    STATE = ROOT / ".state"
    WORK = STATE / "txt_uploads"
    EXPORTS = ROOT / "exports"
    TOPICS = STATE / "batch_forum_topics.json"
    BACKUPS = STATE / "sync_backups"


configure("/root/unac")


class SyncError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data, ascii_safe=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(data, indent=2, ensure_ascii=not ascii_safe),
        encoding="utf-8",
    )
    temp.replace(path)


def _write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _valid_receipt(data):
    if not isinstance(data, dict):
        return False
    uploaded = data.get("uploaded", {})
    if not isinstance(uploaded, dict):
        return False
    for kind, item in uploaded.items():
        if not isinstance(kind, str) or not isinstance(item, dict):
            return False
        mid = item.get("message_id")
        if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0:
            return False
    return True


def _mid(item):
    return int(item.get("message_id") or 0)


# ------------------------------------------------------------------- merge

SPECIAL = {
    "uploaded", "complete", "video_complete", "board_complete",
    "slides_zip_version", "asset_download_errors",
}


def merge_receipt(local, incoming):
    """Additive, deterministic merge of two receipts of the same lecture."""
    up_l = local.get("uploaded") or {}
    up_i = incoming.get("uploaded") or {}

    uploaded = {}
    for kind in sorted(set(up_l) | set(up_i)):
        a, b = up_l.get(kind), up_i.get(kind)
        if a and b:
            # Same file uploaded twice: keep the earlier message.
            uploaded[kind] = a if _mid(a) <= _mid(b) else b
        else:
            uploaded[kind] = a or b

    out = {}
    for source in (incoming, local):  # local wins for plain fields
        for key, value in source.items():
            if key not in SPECIAL:
                out[key] = value

    out["uploaded"] = uploaded

    def has(key):
        return key in local or key in incoming

    # Only touch fields that exist on at least one side, so merging never
    # invents keys (keeps both servers' receipts identical after sync).
    if has("complete"):
        out["complete"] = bool(
            local.get("complete") or incoming.get("complete")
        )

    if has("video_complete"):
        out["video_complete"] = (
            "video" in uploaded
            or bool(local.get("video_complete"))
            or bool(incoming.get("video_complete"))
        )

    if has("board_complete"):
        out["board_complete"] = (
            "board" in uploaded
            or bool(local.get("board_complete"))
            or bool(incoming.get("board_complete"))
        )

    if has("slides_zip_version"):
        out["slides_zip_version"] = max(
            int(local.get("slides_zip_version") or 0),
            int(incoming.get("slides_zip_version") or 0),
        )

    if has("asset_download_errors"):
        if out.get("complete"):
            out["asset_download_errors"] = []
        else:
            errors = set(
                str(e)
                for x in (local, incoming)
                for e in (x.get("asset_download_errors") or [])
            )
            out["asset_download_errors"] = sorted(errors)[:30]

    return out


def merge_topics(local, incoming):
    out = dict(local)
    added, conflicts = {}, []

    for key, value in incoming.items():
        if not isinstance(key, str) or not TOPIC_KEY.match(key):
            continue
        try:
            topic = int(value)
        except (TypeError, ValueError):
            continue

        if key not in out:
            out[key] = topic
            added[key] = topic
        elif int(out[key]) != topic:
            conflicts.append((key, int(out[key]), topic))

    return out, added, conflicts


# ------------------------------------------------------------------ export

def build_export(dest_zip, group_id):
    counts = {"receipts": 0, "inputs": 0, "exports": 0, "topics": 0}

    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        if WORK.is_dir():
            for path in sorted(WORK.glob("*.json")):
                if not RECEIPT_NAME.match(path.name):
                    continue
                if path.stat().st_size > MAX_MEMBER:
                    continue
                if _valid_receipt(_read_json(path)):
                    z.write(path, f"receipts/{path.name}")
                    counts["receipts"] += 1

            for path in sorted(WORK.glob("input_*.txt")):
                if (
                    INPUT_NAME.match(path.name)
                    and path.stat().st_size <= MAX_MEMBER
                ):
                    z.write(path, f"inputs/{path.name}")
                    counts["inputs"] += 1

        if EXPORTS.is_dir():
            seen = set()
            for path in sorted(EXPORTS.rglob("*.txt")):
                if (
                    path.name in seen
                    or not EXPORT_NAME.match(path.name)
                    or path.stat().st_size > MAX_MEMBER
                ):
                    continue
                seen.add(path.name)
                z.write(path, f"exports/{path.name}")
                counts["exports"] += 1

        topics = _read_json(TOPICS, {})
        if isinstance(topics, dict) and topics:
            z.writestr(
                "topics.json",
                json.dumps(topics, indent=2, ensure_ascii=False),
            )
            counts["topics"] = len(topics)

        z.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": FORMAT,
                    "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "group_id": str(group_id),
                    "counts": counts,
                },
                indent=2,
            ),
        )

    return counts


def make_backup(group_id):
    BACKUPS.mkdir(parents=True, exist_ok=True)
    name = f"pre_import_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    path = BACKUPS / name
    build_export(path, group_id)

    old = sorted(BACKUPS.glob("pre_import_*.zip"))[:-KEEP_BACKUPS]
    for item in old:
        item.unlink(missing_ok=True)

    return name


# ------------------------------------------------------------------ import

def _local_text_hashes():
    hashes = set()
    folders = []
    if WORK.is_dir():
        folders.append(WORK.glob("input_*.txt"))
    if EXPORTS.is_dir():
        folders.append(EXPORTS.rglob("*.txt"))

    for folder in folders:
        for path in folder:
            try:
                hashes.add(_sha(path.read_bytes()))
            except OSError:
                continue
    return hashes


def _unique_path(folder, name, digest):
    target = Path(folder) / name
    if not target.exists():
        return target
    stem, suffix = os.path.splitext(name)
    return Path(folder) / f"{stem}__{digest[:8]}{suffix}"


def apply_import(zip_path, group_id):
    zip_path = Path(zip_path)

    if zip_path.stat().st_size > MAX_ZIP:
        raise SyncError("ZIP bahut bada hai (max 100 MB).")

    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise SyncError("Ye valid ZIP file nahi hai.")

    with archive:
        infos = archive.infolist()

        if len(infos) > MAX_FILES:
            raise SyncError("ZIP me bahut zyada files hain.")
        if sum(i.file_size for i in infos) > MAX_TOTAL:
            raise SyncError("ZIP unpack hone par bahut bada hai.")

        names = {i.filename for i in infos}
        if "manifest.json" not in names:
            raise SyncError(
                "manifest.json nahi mila. Ye bot ka export ZIP nahi hai."
            )

        try:
            manifest = json.loads(archive.read("manifest.json"))
        except ValueError:
            raise SyncError("manifest.json kharab hai.")

        if manifest.get("format") != FORMAT:
            raise SyncError("ZIP ka format alag hai.")

        if str(manifest.get("group_id")) != str(group_id):
            raise SyncError(
                "Storage group alag hai "
                f"(ZIP: {manifest.get('group_id')}, yahan: {group_id}). "
                "Message IDs match nahi honge, isliye import roka."
            )

        backup = make_backup(group_id)

        report = {
            "backup": backup,
            "receipts_added": 0,
            "receipts_merged": 0,
            "receipts_same": 0,
            "receipts_skipped": 0,
            "topics_added": 0,
            "topic_conflicts": [],
            "txt_added": 0,
            "txt_duplicate": 0,
        }

        WORK.mkdir(parents=True, exist_ok=True)

        # receipts
        for info in infos:
            match = re.fullmatch(
                r"receipts/([0-9a-f]{64}\.json)", info.filename
            )
            if not match:
                continue

            if info.file_size > MAX_MEMBER:
                report["receipts_skipped"] += 1
                continue

            try:
                incoming = json.loads(archive.read(info))
            except ValueError:
                report["receipts_skipped"] += 1
                continue

            if not _valid_receipt(incoming):
                report["receipts_skipped"] += 1
                continue

            target = WORK / match.group(1)
            local = _read_json(target, None) if target.exists() else None

            if not isinstance(local, dict) or not _valid_receipt(local):
                _write_json(target, incoming)
                report["receipts_added"] += 1
                continue

            merged = merge_receipt(local, incoming)

            if merged == local:
                report["receipts_same"] += 1
            else:
                _write_json(target, merged)
                report["receipts_merged"] += 1

        # topic map
        if "topics.json" in names:
            try:
                incoming_topics = json.loads(archive.read("topics.json"))
            except ValueError:
                incoming_topics = {}

            if isinstance(incoming_topics, dict):
                local_topics = _read_json(TOPICS, {})
                if not isinstance(local_topics, dict):
                    local_topics = {}

                merged_topics, added, conflicts = merge_topics(
                    local_topics, incoming_topics
                )
                if added:
                    _write_json(TOPICS, merged_topics, ascii_safe=False)

                report["topics_added"] = len(added)
                report["topic_conflicts"] = conflicts

        # TXT files (dedupe by content)
        hashes = _local_text_hashes()

        for info in infos:
            for pattern, folder in (
                (r"inputs/(input_[A-Za-z0-9_.-]{1,100}\.txt)", WORK),
                (r"exports/([A-Za-z0-9_.' ()\-]{1,160}\.txt)", EXPORTS),
            ):
                match = re.fullmatch(pattern, info.filename)
                if not match:
                    continue
                if info.file_size > MAX_MEMBER:
                    break

                data = archive.read(info)
                digest = _sha(data)

                if digest in hashes:
                    report["txt_duplicate"] += 1
                else:
                    _write_bytes(
                        _unique_path(folder, match.group(1), digest), data
                    )
                    hashes.add(digest)
                    report["txt_added"] += 1
                break

    return report


def format_report(report):
    lines = [
        "✅ Import complete",
        "",
        f"Receipts: {report['receipts_added']} naye, "
        f"{report['receipts_merged']} merge hue, "
        f"{report['receipts_same']} pehle se same",
    ]

    if report["receipts_skipped"]:
        lines.append(f"Skip (kharab): {report['receipts_skipped']}")

    lines.append(f"Batch topics: {report['topics_added']} naye")
    lines.append(
        f"TXT files: {report['txt_added']} nayi, "
        f"{report['txt_duplicate']} duplicate (skip)"
    )

    if report["topic_conflicts"]:
        lines += ["", "⚠️ Topic conflict (dono jagah alag topic bana tha):"]
        for key, mine, theirs in report["topic_conflicts"][:5]:
            lines.append(f"• {key.partition(':')[2][:60]}: yahan {mine}, ZIP me {theirs}")
        lines.append("Yahan wala topic hi rakha gaya hai.")

    lines += [
        "",
        f"💾 Backup: {report['backup']}",
        "Stream server 30 second me naye videos pakad lega.",
    ]
    return "\n".join(lines)


# ----------------------------------------------------------- Telegram hook

pending_import = set()


async def _do_import(event, owner, group_id):
    name = (getattr(event.file, "name", "") or "").lower()

    if not name.endswith(".zip"):
        await event.respond("Sirf .zip file bhejo (export wali).")
        return

    if (event.file.size or 0) > MAX_ZIP:
        await event.respond("ZIP maximum 100 MB.")
        return

    await event.respond("⏳ Import ho raha hai...")

    tmp = Path(tempfile.mkdtemp(prefix="unac_import_"))
    try:
        path = tmp / "in.zip"
        await event.download_media(file=str(path))
        report = await asyncio.to_thread(apply_import, path, group_id)
        await event.respond(format_report(report))
    except SyncError as error:
        await event.respond(f"❌ {error}")
    except Exception as error:
        await event.respond(
            f"❌ Import fail: {type(error).__name__}: {str(error)[:150]}"
        )
    finally:
        pending_import.discard(owner)
        shutil.rmtree(tmp, ignore_errors=True)


async def handle_sync_event(event, client, owner, upload_active):
    """Returns True when the message was handled here."""
    text = (event.raw_text or "").strip()
    low = text.lower()
    has_file = bool(getattr(event.message, "document", None))
    group_id = os.environ.get("STORAGE_GROUP_ID", "")

    if low == "/exportdata":
        await event.respond("⏳ Export ban raha hai...")
        tmp = Path(tempfile.mkdtemp(prefix="unac_export_"))
        try:
            path = tmp / f"unac_sync_{time.strftime('%Y%m%d_%H%M%S')}.zip"
            counts = await asyncio.to_thread(build_export, path, group_id)
            await client.send_file(
                event.chat_id,
                str(path),
                force_document=True,
                caption=(
                    "📦 Uploader data export\n\n"
                    f"Receipts: {counts['receipts']}\n"
                    f"Batch topics: {counts['topics']}\n"
                    f"TXT files: {counts['exports'] + counts['inputs']}\n\n"
                    "Dusre server ke bot me ye ZIP bhejkar /importdata likho."
                ),
            )
        except Exception as error:
            await event.respond(
                f"❌ Export fail: {type(error).__name__}: {str(error)[:150]}"
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return True

    if low == "/importcancel":
        if owner in pending_import:
            pending_import.discard(owner)
            await event.respond("❌ Import cancel.")
        else:
            await event.respond("Koi import pending nahi hai.")
        return True

    if low == "/importdata":
        if upload_active:
            await event.respond(
                "Upload chal raha hai. Pehle /uploadstop karo, "
                "phir /importdata."
            )
            return True

        if has_file:
            await _do_import(event, owner, group_id)
            return True

        pending_import.add(owner)
        await event.respond(
            "📥 Export ZIP file bhejo.\n\n"
            "Ye data merge karta hai, purana kuch delete ya overwrite nahi "
            "hota, aur import se pehle backup ban jata hai.\n\n"
            "Cancel: /importcancel"
        )
        return True

    if owner in pending_import and has_file:
        if upload_active:
            await event.respond(
                "Upload chal raha hai. /uploadstop karke dobara bhejo."
            )
            return True
        await _do_import(event, owner, group_id)
        return True

    return False
