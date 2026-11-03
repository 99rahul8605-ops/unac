"""Independent, owner-only StudyUK /txt browser and metadata TXT generator.

This module is deliberately isolated from the legacy HRVertex catalog and from
/uploadtxt. It uses only publicly available HTML/board JSON and does not try to
circumvent authentication, encryption, or access controls.
"""

import asyncio
import html
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, parse_qs

import aiohttp
from telethon import Button

BASE = 'https://studyuk.online'
GOALS_PAGE = BASE + '/goal/index.php?change=1'
ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / 'exports'
EXPORTS.mkdir(parents=True, exist_ok=True)
TIMEOUT = aiohttp.ClientTimeout(total=35, connect=12)
STATES = {}
MAX_LECTURES = 1000
MAX_SLIDES = 250  # Matches studyuk_uploader.py


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        self.parts = []

    def text(self):
        result = list(self.parts)
        for ch in self.children:
            result.append(ch.text())
        return ' '.join(x for x in result if x)

    def parent_text(self):
        for p in (self.parent, self.parent.parent if self.parent else None):
            if p:
                v = ' '.join(p.text().split())
                if v and len(v) < 450:
                    return v
        return ' '.join(self.text().split())


class DOM(HTMLParser):
    VOID = {'meta', 'link', 'img', 'input', 'hr', 'br', 'source', 'area', 'base', 'embed', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node('root')
        self.stack = [self.root]
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        n = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(n)
        self.nodes.append(n)
        if tag not in self.VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, text):
        if text.strip():
            self.stack[-1].parts.append(text)


def trim(value):
    return ' '.join(html.unescape(str(value or '')).split()).strip()


def safe_filename(value):
    return re.sub(r'[^a-zA-Z0-9_-]+', '_', trim(value))[:64].strip('_') or 'studyuk'


def validate_site_url(url, expected_path=None):
    url = urljoin(BASE, html.unescape(url))
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != 'studyuk.online' or p.username or p.password:
        raise ValueError('Untrusted site URL')
    if expected_path and not p.path.endswith(expected_path):
        raise ValueError('Unexpected site page')
    return url


async def fetch_text(url):
    url = validate_site_url(url)
    async with aiohttp.ClientSession(timeout=TIMEOUT, headers={'User-Agent': 'Mozilla/5.0 (compatible; LectureManager/1.0)'}) as session:
        async with session.get(url, allow_redirects=True) as r:
            validate_site_url(str(r.url))
            r.raise_for_status()
            return await r.text(errors='replace')


def parse_directory(raw, kind, page_url):
    dom = DOM()
    dom.feed(raw)
    page = urlsplit(page_url)
    output = []
    seen = set()
    dest = {'goal': 'teachers.php', 'teacher': 'courses.php', 'course': 'content.php'}[kind]
    for n in dom.nodes:
        if n.tag != 'a':
            continue
        href = n.attrs.get('href') or ''
        try:
            url = validate_site_url(urljoin(page_url, href), dest)
        except (ValueError, TypeError):
            continue
        if url in seen:
            continue
        seen.add(url)
        q = parse_qs(urlsplit(url).query)
        text = trim(n.parent_text())
        # CTA text is not a useful title; use identifying query metadata.
        if kind == 'goal':
            name = q.get('goal', [''])[0] or text
        elif kind == 'teacher':
            name = q.get('teacher', [''])[0] or text
        else:
            name = q.get('title', [''])[0] or text
        if not name:
            continue
        output.append({'name': trim(name)[:160], 'url': url})
    return output


def _parse_call(raw):
    # Button onclick uses JS string arguments and includes the URL and title.
    m = re.search(r'openVid\(\s*([\'\"])(https://uamedia\.uacdn\.net/lesson-raw/[A-Za-z0-9_-]+/output\.(?:webm|mp4))\1\s*,\s*([\'\"])(.*?)\3\s*\)', raw, re.S)
    if not m:
        return None
    return m.group(2), trim(m.group(4).replace('\\\'', "'").replace('\\"', '"'))


def parse_lessons(raw):
    dom = DOM()
    dom.feed(raw)
    rows = []
    seen = set()
    for n in dom.nodes:
        onclick = html.unescape(n.attrs.get('onclick', ''))
        pair = _parse_call(onclick)
        if not pair:
            continue
        video, title = pair
        lesson = re.search(r'/lesson-raw/([A-Za-z0-9_-]+)/', video).group(1)
        if lesson in seen:
            continue
        seen.add(lesson)
        # Find PDF in the nearest containing lesson/card; do not mix PDFs
        # from adjacent rows when a page contains many lecture cards.
        pdf = ''
        p = n.parent
        for _ in range(3):
            if p is None:
                break
            urls = [el.attrs.get('href', '') for el in walk(p) if el.tag == 'a']
            for href in urls:
                if f'/slides_pdf/{lesson}/' in href:
                    pdf = urljoin('https://player.uacdn.net', html.unescape(href))
                    break
            if pdf:
                break
            p = p.parent
        rows.append({'name': title or f'Lecture {len(rows)+1}', 'lesson_id': lesson,
                     'video_url': video,
                     'board_url': f'https://uamedia.uacdn.net/lesson-raw/{lesson}/data.json',
                     'pdf_url': pdf, 'slides': [], 'error': ''})
    return rows


def walk(node):
    yield node
    for child in node.children:
        yield from walk(child)


def extract_slides(board):
    # Keep chronological first-occurrence order, exactly like source TXT.
    urls, seen, stack = [], set(), [board]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(reversed(node))
        elif isinstance(node, dict):
            if node.get('plugin') == 'dcn':
                dat = node.get('data') or {}
                url = dat.get('u') if dat.get('e') == 'as' else None
                if isinstance(url, str) and urlsplit(url).hostname == 'uadoc.uacdn.net' and url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


async def fill_slides(rows, progress):
    sem = asyncio.Semaphore(3)
    count = 0
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=80, connect=15)) as session:
        async def task(index, row):
            nonlocal count
            async with sem:
                for attempt in range(2):
                    try:
                        async with session.get(row['board_url']) as resp:
                            resp.raise_for_status()
                            board = await resp.json(content_type=None)
                        row['slides'] = extract_slides(board)
                        if len(row['slides']) > MAX_SLIDES:
                            row['error'] = f'Too many slides ({len(row["slides"])} > {MAX_SLIDES})'
                        break
                    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                        if attempt == 1:
                            row['error'] = f'{type(exc).__name__}: {str(exc)[:120]}'
                        else:
                            await asyncio.sleep(1)
                count += 1
                if count % 5 == 0 or count == len(rows):
                    try:
                        await progress.edit(f'📄 Reading board slides: {count}/{len(rows)}')
                    except Exception:
                        pass
        await asyncio.gather(*(task(i, row) for i, row in enumerate(rows)))


def generate_lines(goal, teacher, course, url, rows):
    lines = [f'GOAL: {trim(goal)}', f'TEACHER: {trim(teacher)}',
             f'COURSE: {trim(course)}', f'COURSE_URL: {url}',
             f'TOTAL_LECTURES: {len(rows)}', '']
    for i, row in enumerate(rows, 1):
        lines.extend([f'CLASS: {i}', f'LECTURE: {row["name"]}',
                      f'LESSON_ID: {row["lesson_id"]}', f'VIDEO_URL: {row["video_url"]}',
                      f'BOARD_URL: {row["board_url"]}', f'PDF_URL: {row["pdf_url"]}',
                      f'SLIDES_COUNT: {len(row["slides"])}'])
        lines.extend(f'SLIDE_URL_{j}: {u}' for j, u in enumerate(row['slides'], 1))
        if row['error']:
            lines.append(f'SLIDES_ERROR: {row["error"]}')
        lines.append('')
    return '\n'.join(lines)


async def _show(event, state, edit=False):
    step = state['step']
    items = state['items']
    page = state.get('page', 0)
    start = page * 10
    label = {'goal':'Goal','teacher':'Teacher','course':'Course'}[step]
    text = f'📄 StudyUK — Select {label}\n\n' + '\n'.join(
        f'{i+1}. {items[i]["name"][:100]}'
        for i in range(start, min(start + 10, len(items))))
    text += f'\n\nPage {page+1}/{max(1,(len(items)+9)//10)} | Total {len(items)}'
    buttons = [[Button.inline(f'{i+1}. {items[i]["name"][:36]}', f'skt:pick:{i}'.encode())]
               for i in range(start, min(start + 10, len(items)))]
    navigation = []
    if page > 0:
        navigation.append(Button.inline('⬅️', b'skt:prev'))
    if start + 10 < len(items):
        navigation.append(Button.inline('➡️', b'skt:next'))
    if navigation:
        buttons.append(navigation)
    buttons.append([Button.inline('❌ Cancel', b'skt:cancel')])
    if edit:
        await event.edit(text, buttons=buttons)
    else:
        await event.respond(text, buttons=buttons)


async def start(event, owner):
    STATES[owner] = {'step': 'search_goal', 'busy': False}
    await event.respond('🎯 StudyUK Goal search\n\nGoal ka naam bhejo, jaise: CA Intermediate / CA Foundation.\nCancel: /txtcancel')


async def handle_message(event, owner):
    state = STATES.get(owner)
    if not state:
        return False
    msg = (event.raw_text or '').strip()
    if msg.lower() in ('/txtcancel', '/cancel'):
        STATES.pop(owner, None)
        await event.respond('❌ StudyUK TXT cancelled.')
        return True
    if state['step'] != 'search_goal':
        return False
    if msg.startswith('/'):
        return False
    if state.get('busy'):
        await event.respond('Previous search is still running.')
        return True
    state['busy'] = True
    try:
        raw = await fetch_text(GOALS_PAGE)
        items = parse_directory(raw, 'goal', GOALS_PAGE)
        query = msg.casefold()
        items = [item for item in items if query in item['name'].casefold()]
        if not items:
            await event.respond('No matching StudyUK goal. Try another keyword.')
            return True
        if STATES.get(owner) is not state:
            return True
        state.update(step='goal', items=items, page=0)
        await _show(event, state)
    except Exception as exc:
        logging.exception('StudyUK goal discovery failed')
        await event.respond(f'❌ StudyUK goal fetch failed: {type(exc).__name__}: {str(exc)[:130]}')
    finally:
        state['busy'] = False
    return True


async def handle_callback(event, owner):
    data = (event.data or b'').decode('utf-8', 'replace')
    if not data.startswith('skt:'):
        return False
    if event.sender_id != owner:
        await event.answer('Not authorized', alert=True)
        return True
    state = STATES.get(owner)
    if data == 'skt:cancel':
        STATES.pop(owner, None)
        await event.answer('Cancelled')
        await event.respond('StudyUK TXT cancelled.')
        return True
    if not state or state.get('step') not in ('goal', 'teacher', 'course'):
        await event.answer('Session expired. Send /txt', alert=True)
        return True
    if state.get('busy'):
        await event.answer('Previous request still running')
        return True
    if data in ('skt:next', 'skt:prev'):
        state['page'] = max(0, min((len(state['items']) - 1)//10,
                              state['page'] + (1 if data == 'skt:next' else -1)))
        await event.answer()
        await _show(event, state, edit=True)
        return True
    if not data.startswith('skt:pick:'):
        await event.answer('Invalid button')
        return True
    try:
        idx = int(data.rsplit(':', 1)[1])
        picked = state['items'][idx]
    except (IndexError, ValueError):
        await event.answer('Invalid selection')
        return True
    await event.answer()
    state['busy'] = True
    step = state['step']
    try:
        if step == 'goal':
            state['goal'] = picked['name']
            items = parse_directory(await fetch_text(picked['url']), 'teacher', picked['url'])
            next_step = 'teacher'
        elif step == 'teacher':
            state['teacher'] = picked['name']
            items = parse_directory(await fetch_text(picked['url']), 'course', picked['url'])
            next_step = 'course'
        else:
            progress = await event.respond('📄 Reading course lectures...')
            raw = await fetch_text(picked['url'])
            rows = parse_lessons(raw)
            if not rows:
                await progress.edit('❌ No lecture links found on this course page.')
                return True
            if len(rows) > MAX_LECTURES:
                await progress.edit(f'❌ Course has too many lectures ({len(rows)}).')
                return True
            await fill_slides(rows, progress)
            lines = generate_lines(state['goal'], state['teacher'], picked['name'], picked['url'], rows)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = EXPORTS / f'studyuk_{safe_filename(picked["name"])}_{stamp}.txt'
            filename.write_text(lines, encoding='utf-8')
            errors = sum(bool(r['error']) for r in rows)
            try:
                await event.client.send_file(event.chat_id, str(filename),
                    caption=f'📄 {picked["name"][:100]}\nLectures: {len(rows)}\nSlide fetch errors: {errors}')
                await progress.edit(f'✅ TXT sent: {len(rows)} lectures; board errors: {errors}')
            finally:
                STATES.pop(owner, None)
            return True
        if not items:
            await event.respond(f'No {next_step}s found on this page.')
            return True
        if STATES.get(owner) is not state:
            return True
        state.update(step=next_step, items=items, page=0)
        await _show(event, state)
    except Exception as exc:
        logging.exception('StudyUK TXT flow failed')
        await event.respond(f'❌ StudyUK TXT error: {type(exc).__name__}: {str(exc)[:180]}')
    finally:
        state['busy'] = False
    return True
