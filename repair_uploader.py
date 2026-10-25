import ast
from pathlib import Path

p = Path("txt_uploader.py")
s = p.read_text()

if "UPLOADER_REPAIR_V3" in s:
    raise SystemExit("✅ Repair already installed")

tree = ast.parse(s)

def replace_function(source, name, code):
    tree = ast.parse(source)
    node = next(
        (n for n in tree.body
         if isinstance(n, ast.AsyncFunctionDef)
         and n.name == name),
        None,
    )
    if node is None:
        raise RuntimeError(f"{name} not found; files unchanged")

    lines = source.splitlines(keepends=True)
    lines[node.lineno - 1:node.end_lineno] = [
        code.rstrip() + "\n\n"
    ]
    return "".join(lines)


ticker = '''
async def progress_ticker(message):
    # UPLOADER_REPAIR_V3
    last = ""

    while True:
        info = progress_info.copy()

        speed = float(info.get("speed_bps") or 0) / 1048576
        current = float(info.get("current_bytes") or 0) / 1048576
        total = float(info.get("total_bytes") or 0) / 1048576

        eta = info.get("eta_seconds")
        if eta is None:
            eta_text = "Calculating..."
        else:
            eta = max(0, int(eta))
            eta_text = f"{eta // 60}m {eta % 60}s"

        output = (
            "📤 TXT Uploader\\n\\n"
            f"Lecture: {info.get('lecture', '')[:110]}\\n"
            f"Completed: {info.get('done', 0)}/"
            f"{info.get('total', 0)}\\n"
            f"Stage: {info.get('stage', 'Idle')}\\n"
            f"Progress: {info.get('percent', 0)}%\\n"
            f"Speed: {speed:.2f} MB/s\\n"
            f"Transferred: {current:.1f}/{total:.1f} MB\\n"
            f"ETA: {eta_text}"
        )

        if output != last:
            try:
                await message.edit(output)
                last = output
            except Exception:
                pass

        await asyncio.sleep(5)
'''

runner = '''
async def run_upload(client, owner, destination, txt_path):
    # UPLOADER_REPAIR_V3
    global ACTIVE_TOPIC_ID

    ticker = None
    completed = 0
    partial = 0
    failed = 0

    try:
        lectures = parse_txt(txt_path)

        if not lectures:
            await client.send_message(
                owner, "❌ TXT mein valid watch links nahi mile."
            )
            return

        from forum_topics import ensure_batch_topic

        ACTIVE_TOPIC_ID, batch_name = await ensure_batch_topic(
            client, destination, txt_path
        )

        await client.send_message(
            owner,
            f"📁 Batch Forum Topic Ready\\n"
            f"Batch: {batch_name}\\n"
            f"Topic ID: {ACTIVE_TOPIC_ID}\\n"
            "Files isi topic mein upload hongi."
        )

        progress_info.update({
            "stage": "Starting",
            "lecture": "",
            "done": 0,
            "total": len(lectures),
            "percent": 0,
            "speed_bps": 0,
            "current_bytes": 0,
            "total_bytes": 0,
            "eta_seconds": None,
        })

        message = await client.send_message(
            owner,
            f"📤 {len(lectures)} lectures loaded."
        )
        ticker = asyncio.create_task(progress_ticker(message))

        from watch_to_api import resolve_lecture

        for position, lecture in enumerate(lectures, 1):
            url = lecture["watch_url"]
            name = lecture["name"]

            progress_info["lecture"] = (
                f"{position}/{len(lectures)} — {name}"
            )
            progress_info["stage"] = "Checking receipt"
            progress_info["percent"] = 0
            progress_info["speed_bps"] = 0
            progress_info["current_bytes"] = 0
            progress_info["total_bytes"] = 0
            progress_info["eta_seconds"] = None

            try:
                receipt = load_receipt(url)

                if receipt.get("complete"):
                    completed += 1
                    progress_info["done"] = completed
                    continue

                progress_info["stage"] = "Resolving video"
                resolved = await resolve_lecture(lecture)

                if not resolved.get("video_url"):
                    raise RuntimeError(
                        "Video URL not resolved"
                    )

                progress_info["stage"] = "Starting API download"
                job_id = await submit_api(lecture, resolved)
                result = await wait_api(job_id)

                entries = result.get("results") or []

                if not entries:
                    raise RuntimeError(
                        f"API returned no file results: "
                        f"{str(result)[:450]}"
                    )

                problems = []
                expected = set()

                for entry in entries:
                    kind = entry.get("kind") or "unknown"
                    expected.add(kind)

                    if entry.get("status") != "downloaded":
                        problems.append(
                            f"{kind}: download "
                            f"{entry.get('status')} "
                            f"{str(entry.get('error') or '')[:100]}"
                        )
                        continue

                    filename = entry.get("path")

                    if not filename:
                        problems.append(
                            f"{kind}: downloaded but path missing"
                        )
                        continue

                    try:
                        path = prepare_file(
                            validated_file(filename),
                            kind,
                        )

                        await upload_one(
                            client, destination,
                            lecture, url, kind, path
                        )

                    except asyncio.CancelledError:
                        raise

                    except Exception as error:
                        problems.append(
                            f"{kind}: upload "
                            f"{type(error).__name__}: "
                            f"{str(error)[:125]}"
                        )

                receipt = load_receipt(url)
                uploaded = set(
                    receipt.get("uploaded", {})
                )

                video_ok = "video" in uploaded
                board_required = "board" in expected
                board_ok = (
                    not board_required
                    or "board" in uploaded
                )

                missing = sorted(expected - uploaded)
                assets_expected = {
                    k for k in expected
                    if k.startswith("asset_")
                }
                assets_uploaded = assets_expected & uploaded

                # Video/board completion is separate from
                # optional slide/asset completion.
                core_ok = video_ok and board_ok

                # Fully complete ONLY when all API
                # resources have upload receipts.
                receipt["complete"] = (
                    core_ok and not missing
                )
                receipt["video_complete"] = video_ok
                receipt["board_complete"] = board_ok
                receipt["pending_resources"] = missing
                save_receipt(url, receipt)

                if receipt["complete"]:
                    completed += 1
                    progress_info["done"] = completed
                else:
                    if core_ok:
                        partial += 1
                    else:
                        failed += 1

                    details = "\\n".join(
                        problems[:4]
                    ) or "Some resources missing"

                    await client.send_message(
                        owner,
                        f"⚠️ {name[:110]}\\n"
                        f"🎬 Video: {'✅' if video_ok else '❌'}\\n"
                        f"📋 Board: {'✅' if board_ok else '❌'}\\n"
                        f"🖼️ Assets: "
                        f"{len(assets_uploaded)}/"
                        f"{len(assets_expected)}\\n"
                        f"📦 Total uploaded: "
                        f"{len(uploaded)}/{len(expected)}\\n"
                        f"❗ Missing: {len(missing)}\\n\\n"
                        f"Errors:\\n{details[:550]}"
                    )

            except asyncio.CancelledError:
                raise

            except Exception as error:
                failed += 1
                await client.send_message(
                    owner,
                    f"❌ {name[:110]}\\n"
                    f"{type(error).__name__}: "
                    f"{str(error)[:400]}"
                )

        await client.send_message(
            owner,
            "🏁 TXT Upload Run Finished\\n\\n"
            f"✅ Fully completed: {completed}\\n"
            f"⚠️ Video/board OK, assets pending: {partial}\\n"
            f"❌ Failed: {failed}\\n"
            f"📚 Total attempted: {len(lectures)}"
        )

    finally:
        if ticker is not None:
            ticker.cancel()

        progress_info["stage"] = "Idle"
        txt_path.unlink(missing_ok=True)
'''

if "ACTIVE_TOPIC_ID" not in s:
    raise SystemExit(
        "❌ Forum routing absent; no changes made"
    )

s = replace_function(s, "progress_ticker", ticker)
s = replace_function(s, "run_upload", runner)

# Preserve the existing upload_one(), Pyrogram,
# API downloader, receipts and forum mappings.
compile(s, str(p), "exec")

backup = Path("txt_uploader.py.before_repair_v3")
backup.write_text(p.read_text())
p.write_text(s)

print("✅ Uploader repaired")
print("✅ Video/board status separated")
print("✅ Asset failures show actual reasons")
print("✅ Telegram progress newlines fixed")
print("✅ Existing receipts preserved")
print("✅ Backup:", backup)
