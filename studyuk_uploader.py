"""Direct-resource StudyUK uploader; independent of the legacy HRVertex resolver.

The source TXT is a user-supplied inventory of permitted media URLs. This module
never changes the legacy ZIP naming or repair rules. Every ZIP entry maps back
to its EXACT Board URL so duplicate PDF page numbers cannot overwrite slides.
"""

import asyncio
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import requests

MAX_SLIDES = 250  # foundation_slides.zip_entries() has the same per-lecture limit
MEDIA_HOST = 'uamedia.uacdn.net'
SLIDE_HOST = 'uadoc.uacdn.net'


def is_studyuk_txt(path):
    try:
        text = Path(path).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeError):
        return False
    return bool(re.search(r'(?im)^VIDEO_URL:\s*https://uamedia\.uacdn\.net/lesson-raw/', text))


def _url(value, hostname, path_pattern=None):
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname != hostname or parsed.username or parsed.password:
        raise ValueError(f'Unsupported resource host/scheme: {value[:150]}')
    if path_pattern and not re.fullmatch(path_pattern, parsed.path):
        raise ValueError(f'Unexpected resource path: {parsed.path[:150]}')
    return value


def parse_studyuk(path):
    """Parse the uploaded TXT, preserving order and all source URLs."""
    raw = Path(path).read_text(encoding='utf-8-sig')
    header = {}
    items = []
    current = None

    def value_of(line):
        return line.partition(':')[2].strip()

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or ':' not in line:
            continue
        key = line.partition(':')[0].strip().upper()
        value = value_of(line)
        if key in ('BATCH', 'COURSE', 'GOAL', 'TEACHER', 'SUBJECT', 'COURSE_URL'):
            header[key] = value
        elif key == 'CLASS':
            if current and current.get('video_url'):
                items.append(current)
            current = {'number': value, 'title': '', 'video_url': '',
                       'board_url': '', 'pdf_url': '', 'slides': []}
        elif current is not None:
            if key == 'LECTURE':
                current['title'] = value
            elif key == 'VIDEO_URL':
                current['video_url'] = value
            elif key == 'BOARD_URL':
                current['board_url'] = value
            elif key == 'PDF_URL':
                current['pdf_url'] = value
            elif re.fullmatch(r'SLIDE_URL_\d+', key):
                if value and value not in current['slides']:
                    current['slides'].append(value)
    if current and current.get('video_url'):
        items.append(current)

    if not items:
        raise ValueError('No StudyUK lectures found in TXT')
    if len(items) > 1000:
        raise ValueError('Maximum 1000 lectures per TXT')
    batch = header.get('BATCH') or header.get('COURSE') or 'StudyUK Lectures'
    subject = header.get('SUBJECT') or header.get('TEACHER') or header.get('COURSE') or 'Course Lectures'
    # Avoid old forum/topic mismatch by deriving these once and saving them
    # in the stored source TXT before upload starts.
    unique = set()
    for index, item in enumerate(items, 1):
        item['name'] = item['title'] or f'Lecture {index}'
        item['subject'] = subject
        item['class_number'] = item['number'] or str(index)
        item['txt_index'] = index
        item['txt_total'] = len(items)
        video = _url(item['video_url'], MEDIA_HOST, r'/lesson-raw/[A-Za-z0-9_-]+/output\.(?:webm|mp4)')
        lesson_id = video.split('/lesson-raw/', 1)[1].split('/', 1)[0]
        expected_board = f'https://{MEDIA_HOST}/lesson-raw/{lesson_id}/data.json'
        item['board_url'] = _url(item['board_url'] or expected_board, MEDIA_HOST,
                                 r'/lesson-raw/[A-Za-z0-9_-]+/data\.json')
        if item['board_url'] != expected_board:
            raise ValueError(f'Video and Board lesson ID mismatch: {item["name"]}')
        for slide in item['slides']:
            _url(slide, SLIDE_HOST)
        if video in unique:
            raise ValueError(f'Duplicate VIDEO_URL: {video}')
        unique.add(video)
        item['watch_url'] = video  # stable ID/receipt key, not a fake /watch page
    return batch.strip()[:128], subject.strip()[:128], items, raw


def normalize_source(path, batch, subject, rows, original):
    """Create stream-index-compatible source from unchanged StudyUK data."""
    if re.search(r'(?im)^WATCH_URL:\s*https://', original):
        # On repeat runs do not append duplicate synthetic entries.
        return
    lines = [f'BATCH: {batch}', f'SUBJECT: {subject}',
             'SOURCE_FORMAT: STUDYUK_DIRECT_V1', '']
    for item in rows:
        lines.extend([
            f'CLASS: {item["class_number"]}',
            f'LECTURE: {item["name"]}',
            f'WATCH_URL: {item["watch_url"]}',
            f'VIDEO_URL: {item["video_url"]}',
            f'BOARD_URL: {item["board_url"]}',
            f'PDF_URL: {item["pdf_url"]}',
            f'SLIDES_COUNT: {len(item["slides"])}',
        ])
        lines.extend(f'SLIDE_URL_{j}: {url}' for j, url in enumerate(item['slides'], 1))
        lines.append('')
    Path(path).write_text('\n'.join(lines), encoding='utf-8')


def _board_urls(path):
    """Read the Board's authoritative URLs, in timeline order."""
    board = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    stack = [board]
    urls = set()
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            if node.get('plugin') == 'dcn' and isinstance(node.get('data'), dict):
                d = node['data']
                if d.get('e') == 'as' and isinstance(d.get('u'), str) and d['u']:
                    if urlsplit(d['u']).hostname == SLIDE_HOST:
                        urls.add(d['u'])
            elif 'plugin' not in node:
                stack.extend(reversed(list(node.values())))
    return urls


def _image_format(path):
    with open(path, 'rb') as f:
        head = f.read(16)
    if head.startswith(b'\xff\xd8\xff'): return 'jpg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'): return 'png'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP': return 'webp'
    if head.startswith((b'GIF87a', b'GIF89a')): return 'gif'
    if head.startswith(b'%PDF-'): return 'pdf'
    raise ValueError(f'Asset is not an image/PDF: {path.name}')


def create_zip(dest, assets):
    """Unique names; store mapping values as original URLs, never page-only keys."""
    mapping = {}
    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for number, (url, source) in enumerate(assets, 1):
            fmt = _image_format(source)
            if fmt == 'pdf':
                # PDF is an actual PDF, not a JPEG response from a .pdf?page URL.
                # Render the first page only; normal page URLs already return JPEG bytes.
                import fitz
                with fitz.open(source) as doc:
                    if doc.needs_pass or len(doc) != 1:
                        raise ValueError(f'Expected single image page, got PDF with {len(doc)} pages: {url}')
                    p = doc[0]
                    scale = min(2., 1280./p.rect.width, 1280./p.rect.height)
                    content = p.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False).tobytes('jpeg', jpg_quality=83)
                    fmt = 'jpg'
            else:
                content = source.read_bytes()
            name = f'source_{number:04d}.{fmt}'
            archive.writestr(name, content)
            mapping[name] = url
        archive.writestr('_slides_mapping.json', json.dumps(mapping, ensure_ascii=False))
    with zipfile.ZipFile(dest) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f'ZIP integrity error: {bad}')
    return len(mapping)


async def run_studyuk_upload(client, owner, destination, txt_path, *, progress_info,
                             upload_one, wait_api, api_request, load_receipt,
                             save_receipt, work, api_root, validated_file):
    """Read direct URLs in <=30-resource API batches; upload video, board and ZIP."""
    from pyrogram.errors import FloodWait
    import txt_uploader as legacy
    from forum_topics import ensure_batch_topic

    batch, subject, lectures, original = parse_studyuk(txt_path)
    normalize_source(txt_path, batch, subject, lectures, original)
    # txt_uploader deletes input_*.txt on completion. Foundation streaming
    # must still be able to find the exact source URLs after a restart.
    export_dir = Path(legacy.ROOT) / 'exports'
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / ('studyuk_' + hashlib.sha256(batch.encode()).hexdigest()[:16] + '.txt')
    export_tmp = export_path.with_suffix('.tmp')
    export_tmp.write_bytes(Path(txt_path).read_bytes())
    export_tmp.replace(export_path)

    # txt_uploader.upload_one uses its own ACTIVE_TOPIC_ID global.
    legacy.ACTIVE_TOPIC_ID, batch_name = await ensure_batch_topic(client, destination, txt_path)
    await client.send_message(owner, f'📁 Batch Topic: {batch_name}\n🎬 Video + 📋 Board + 📦 One Slides ZIP\nNew source: StudyUK')
    progress_info.update(stage='Starting StudyUK', lecture='', done=0,
                         total=len(lectures), percent=0, speed_bps=0,
                         current_bytes=0, total_bytes=0, eta_seconds=None)

    async def submit_files(lecture, resources):
        payload = {'lecture': lecture['name'], 'watch_url': lecture['watch_url'],
                   'files': resources}
        job = await asyncio.to_thread(api_request, 'POST', '/download', json=payload)
        result = await wait_api(job['job_id'])
        return job['job_id'], result.get('results') or []

    completed = partial = failed = 0
    ticker_message = await client.send_message(owner, f'📦 {len(lectures)} StudyUK lectures loaded')
    ticker = asyncio.create_task(legacy.progress_ticker(ticker_message))
    try:
        for lecture in lectures:
            name, key = lecture['name'], lecture['watch_url']
            progress_info.update(lecture=f'{lecture["txt_index"]}/{len(lectures)} — {name}',
                                 stage='Checking uploads', percent=0,
                                 current_bytes=0, total_bytes=0, speed_bps=0, eta_seconds=None)
            workfiles = []
            problems = []
            try:
                receipt = load_receipt(key)
                if receipt.get('complete') and receipt.get('slides_zip_version') == 4:
                    completed += 1
                    progress_info['done'] = completed
                    continue
                uploaded = receipt.get('uploaded') or {}
                core = []
                if 'video' not in uploaded:
                    core.append({'kind':'video','url':lecture['video_url']})
                if 'board' not in uploaded:
                    core.append({'kind':'board','url':lecture['board_url']})
                board_path = None
                board_urls = None
                if core:
                    progress_info['stage'] = 'Downloading video/board'
                    job_id, entries = await submit_files(lecture, core)
                    for entry in entries:
                        kind = entry.get('kind')
                        if entry.get('status') != 'downloaded' or not entry.get('path'):
                            problems.append(f'{kind}: {entry.get("error") or entry.get("status")}')
                            continue
                        source = validated_file(entry['path'])
                        workfiles.append(source)
                        if kind == 'board':
                            board_path = source
                            # upload_one deletes the local file immediately;
                            # inspect it before uploading.
                            try:
                                board_urls = _board_urls(source)
                            except Exception as err:
                                problems.append(f'Board JSON parse: {err}')
                        try:
                            await upload_one(client, destination, lecture, key, kind, source)
                        except Exception as err:
                            problems.append(f'{kind}: upload {err}')
                uploaded = (load_receipt(key).get('uploaded') or {})
                if 'video' not in uploaded or 'board' not in uploaded:
                    await client.send_message(owner, f'❌ {name}: main Video/Board unavailable; ZIP skipped. ' + '; '.join(problems)[:200])
                    failed += 1
                    # Prevent an out-of-order partially uploaded course.
                    break

                # The TXT contains URLs from the same Board JSON. Preserve
                # exact original URLs (including query parameters) for matching.
                urls = list(lecture['slides'])
                if board_urls is not None:
                    missing = board_urls.difference(urls)
                    if missing:
                        # Recover real Board references, then report any
                        # failed downloads instead of silently losing images.
                        urls.extend(sorted(missing))
                for slide_url in urls:
                    _url(slide_url, SLIDE_HOST)
                if len(urls) > MAX_SLIDES:
                    raise ValueError(f'{len(urls)} slide assets exceed player limit {MAX_SLIDES}; not truncating silently')
                receipt = load_receipt(key)
                if not urls:
                    receipt['slides_zip_version'] = 4
                    receipt['complete'] = not problems
                    receipt['asset_download_errors'] = problems
                    save_receipt(key, receipt)
                    if not problems: completed += 1
                    else: partial += 1
                    progress_info['done'] = completed
                    continue

                # The existing API caps resources at 30. One lecture's slide
                # count may exceed 30, so schedule 25 at a time, sequentially.
                assets = []
                for start in range(0, len(urls), 25):
                    progress_info['stage'] = f'Downloading slides {start+1}-{min(start+25,len(urls))}/{len(urls)}'
                    resources = [{'kind':f'asset_{start+i+1}', 'url':u}
                                 for i,u in enumerate(urls[start:start+25])]
                    _, entries = await submit_files(lecture, resources)
                    by_kind = {e.get('kind'): e for e in entries}
                    for i, url in enumerate(urls[start:start+25], start+1):
                        entry = by_kind.get(f'asset_{i}', {})
                        if entry.get('status') != 'downloaded' or not entry.get('path'):
                            problems.append(f'asset_{i}: {str(entry.get("error") or entry.get("status") or "missing")[:100]}')
                            continue
                        file = validated_file(entry['path'])
                        workfiles.append(file)
                        assets.append((url, file))

                if not assets:
                    problems.append('No slide images downloaded')
                else:
                    folder = work / f'studyuk_zip_{hashlib.sha256(key.encode()).hexdigest()[:12]}'
                    folder.mkdir(parents=True, exist_ok=True)
                    zip_path = folder / 'Lecture_Slides.zip'
                    progress_info['stage'] = f'Packing {len(assets)} slides into ZIP'
                    count = await asyncio.to_thread(create_zip, zip_path, assets)
                    await upload_one(client, destination, lecture, key, 'slides_zip', zip_path)
                    if count != len(urls):
                        problems.append(f'Only {count}/{len(urls)} slide images saved')
                receipt = load_receipt(key)
                uploaded = receipt.get('uploaded') or {}
                complete = not problems and all(x in uploaded for x in ('video','board','slides_zip'))
                receipt.update(complete=complete, slides_zip_version=4,
                               video_complete='video' in uploaded,
                               board_complete='board' in uploaded,
                               asset_download_errors=problems[:30])
                save_receipt(key, receipt)
                if complete:
                    completed += 1
                    progress_info['done'] = completed
                else:
                    partial += 1
                    await client.send_message(owner, f'⚠️ {name}: ' + '; '.join(problems[:3])[:350])
            except asyncio.CancelledError:
                raise
            except FloodWait as err:
                await client.send_message(owner, f'⏳ FloodWait {err.value}s. Run /uploadtxt again to resume.')
                break
            except Exception as err:
                partial += 1
                await client.send_message(owner, f'❌ {name}: {type(err).__name__}: {str(err)[:260]}')
            finally:
                for file in workfiles:
                    file.unlink(missing_ok=True)
        await client.send_message(owner,
            f'🏁 StudyUK upload finished\n✅ Complete: {completed}\n⚠️ Partial: {partial}\n❌ Failed: {failed}')
    finally:
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
