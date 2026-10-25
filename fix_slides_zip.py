import ast
from pathlib import Path

p = Path("txt_uploader.py")
original = p.read_text()

if "SLIDES_SINGLE_ZIP_V1" in original:
    raise SystemExit("Slides ZIP fix already installed")

tree = ast.parse(original)
fn = next(
    (
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef)
        and n.name == "run_upload"
    ),
    None,
)

if fn is None:
    raise SystemExit("run_upload not found; nothing changed")

if "ACTIVE_TOPIC_ID" not in original:
    raise SystemExit("Forum routing missing; nothing changed")

new_runner = '''
async def run_upload(client, owner, destination, txt_path):
    # SLIDES_SINGLE_ZIP_V1
    import zipfile
    from pyrogram.errors import FloodWait

    global ACTIVE_TOPIC_ID

    ticker = None
    completed = 0
    partial = 0
    failed = 0

    try:
        lectures = parse_txt(txt_path)

        if not lectures:
            await client.send_message(
                owner, "No valid watch links in TXT."
            )
            return

        from forum_topics import ensure_batch_topic

        ACTIVE_TOPIC_ID, batch_name = await ensure_batch_topic(
            client, destination, txt_path
        )

        await client.send_message(
            owner,
            f"📁 Batch Topic: {batch_name}\\n"
            f"Topic ID: {ACTIVE_TOPIC_ID}\\n"
            "🎬 Video + 📋 Board + 📦 One Slides ZIP"
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
            owner, f"📦 {len(lectures)} lectures loaded"
        )
        ticker = asyncio.create_task(progress_ticker(message))

        from watch_to_api import resolve_lecture

        for position, lecture in enumerate(lectures, 1):
            url = lecture["watch_url"]
            name = lecture["name"]

            progress_info.update({
                "lecture": f"{position}/{len(lectures)} — {name}",
                "stage": "Checking uploads",
                "percent": 0,
                "speed_bps": 0,
                "current_bytes": 0,
                "total_bytes": 0,
                "eta_seconds": None,
            })

            try:
                receipt = load_receipt(url)

                if (
                    receipt.get("complete")
                    and receipt.get("slides_zip_version") == 1
                ):
                    completed += 1
                    progress_info["done"] = completed
                    continue

                progress_info["stage"] = "Resolving video"
                resolved = await resolve_lecture(lecture)

                if not resolved.get("video_url"):
                    raise RuntimeError("Video URL not resolved")

                progress_info["stage"] = "Downloading resources"
                job_id = await submit_api(lecture, resolved)
                result = await wait_api(job_id)
                entries = result.get("results") or []

                if not entries:
                    raise RuntimeError("API returned no file results")

                problems = []
                slides = []
                asset_failures = []
                required = {"video"}

                if any(
                    x.get("kind") == "board" for x in entries
                ):
                    required.add("board")

                # Video and board stay separate.
                # Assets are collected, NOT uploaded individually.
                for entry in entries:
                    kind = entry.get("kind", "")
                    status = entry.get("status", "")
                    filename = entry.get("path")

                    if kind.startswith("asset_"):
                        if status == "downloaded" and filename:
                            try:
                                path = validated_file(filename)
                                if path.stat().st_size > 0:
                                    slides.append((kind, path))
                                else:
                                    asset_failures.append(
                                        f"{kind}: empty file"
                                    )
                            except Exception as error:
                                asset_failures.append(
                                    f"{kind}: {error}"
                                )
                        else:
                            asset_failures.append(
                                f"{kind}: {status} "
                                f"{str(entry.get('error') or '')[:70]}"
                            )
                        continue

                    if kind not in ("video", "board"):
                        continue

                    if status != "downloaded" or not filename:
                        problems.append(
                            f"{kind}: download {status}"
                        )
                        continue

                    try:
                        path = prepare_file(
                            validated_file(filename), kind
                        )

                        await upload_one(
                            client, destination,
                            lecture, url, kind, path
                        )
                    except asyncio.CancelledError:
                        raise
                    except FloodWait:
                        raise
                    except Exception as error:
                        problems.append(
                            f"{kind}: {type(error).__name__}: {error}"
                        )

                # Create exactly one archive per lecture.
                if slides:
                    folder = API_ROOT / str(job_id)
                    zip_path = folder / "Lecture_Slides.zip"

                    progress_info["stage"] = (
                        f"Packing {len(slides)} slides into ZIP"
                    )

                    def make_zip():
                        with zipfile.ZipFile(
                            zip_path,
                            "w",
                            compression=zipfile.ZIP_STORED,
                            allowZip64=True,
                        ) as archive:
                            for kind, source in slides:
                                archive.write(
                                    source,
                                    arcname=source.name,
                                )

                        with zipfile.ZipFile(zip_path) as archive:
                            bad = archive.testzip()
                            if bad is not None:
                                raise RuntimeError(
                                    f"ZIP integrity failed: {bad}"
                                )

                    try:
                        await asyncio.to_thread(make_zip)

                        await upload_one(
                            client, destination,
                            lecture, url,
                            "slides_zip", zip_path
                        )

                        # Assets can be cleaned only after
                        # their ZIP is safely uploaded.
                        for _, source in slides:
                            source.unlink(missing_ok=True)

                    except asyncio.CancelledError:
                        raise
                    except FloodWait:
                        raise
                    except Exception as error:
                        problems.append(
                            f"slides_zip: "
                            f"{type(error).__name__}: {error}"
                        )

                receipt = load_receipt(url)
                uploaded = set(receipt.get("uploaded", {}))

                video_ok = "video" in uploaded
                board_ok = (
                    "board" not in required
                    or "board" in uploaded
                )
                slides_ok = (
                    not slides
                    or "slides_zip" in uploaded
                )

                # Failed downloads are reported separately.
                fully_ok = (
                    video_ok and board_ok and slides_ok
                    and not asset_failures
                )

                receipt["complete"] = fully_ok
                receipt["slides_zip_version"] = 1
                receipt["video_complete"] = video_ok
                receipt["board_complete"] = board_ok
                receipt["asset_download_errors"] = (
                    asset_failures[:30]
                )
                save_receipt(url, receipt)

                if fully_ok:
                    completed += 1
                    progress_info["done"] = completed
                else:
                    if video_ok and board_ok:
                        partial += 1
                    else:
                        failed += 1

                    details = problems + asset_failures

                    await client.send_message(
                        owner,
                        f"⚠️ {name[:100]}\\n"
                        f"🎬 Video: {'✅' if video_ok else '❌'}\\n"
                        f"📋 Board: {'✅' if board_ok else '❌'}\\n"
                        f"📦 Slides ZIP: "
                        f"{'✅' if 'slides_zip' in uploaded else '❌'}\\n"
                        f"Downloaded assets: {len(slides)}\\n"
                        f"Asset download errors: "
                        f"{len(asset_failures)}\\n"
                        + "\\n".join(details[:3])[:350]
                    )

            except asyncio.CancelledError:
                raise

            except FloodWait as error:
                seconds = int(error.value)
                await client.send_message(
                    owner,
                    f"⏳ Telegram FloodWait: {seconds}s\\n"
                    "Uploader safely stopped. "
                    "Resume after the wait."
                )
                break

            except Exception as error:
                failed += 1
                await client.send_message(
                    owner,
                    f"❌ {name[:100]}\\n"
                    f"{type(error).__name__}: "
                    f"{str(error)[:300]}"
                )

        await client.send_message(
            owner,
            "🏁 Upload finished\\n"
            f"✅ Complete: {completed}\\n"
            f"⚠️ Partial: {partial}\\n"
            f"❌ Failed: {failed}"
        )

    finally:
        if ticker:
            ticker.cancel()

        progress_info["stage"] = "Idle"
        txt_path.unlink(missing_ok=True)
'''

lines = original.splitlines(keepends=True)
lines[fn.lineno - 1:fn.end_lineno] = [
    new_runner.strip() + "\n\n"
]

updated = "".join(lines)
compile(updated, str(p), "exec")

backup = Path("txt_uploader.py.before_slides_zip")
backup.write_text(original)
p.write_text(updated)

print("✅ Single Slides ZIP uploader installed")
print("✅ Individual asset uploads disabled")
print("✅ Pyrogram preserved")
print("✅ Forum Topics preserved")
print("✅ Existing receipts preserved")
print("✅ Backup:", backup)
