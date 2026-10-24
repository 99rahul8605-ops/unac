
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

BASE = "https://unacademy.hrvertex.workers.dev"

GENERIC = re.compile(
    r"^(view classes|view details|view batch|view pack|"
    r"explore|learn more|open|watch|join|enroll|details|back)"
    r"\s*[→➜➡]?$",
    re.I,
)


def identifier(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


def _state_path(profile):
    return Path(profile).parent / "catalog_navigation.json"


def _read_state(profile):
    try:
        return json.loads(_state_path(profile).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(profile, data):
    path = _state_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _remember(profile, rows):
    state = _read_state(profile)

    for item in rows:
        key = item["type"] + ":" + item["uid"]
        state[key] = {
            **state.get(key, {}),
            **item,
        }

    _write_state(profile, state)


def _restore(profile, item):
    key = item["type"] + ":" + item["uid"]
    saved = _read_state(profile).get(key, {})
    return {**saved, **item}


async def start_browser(profile):
    Path(profile).parent.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()

    try:
        context = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            args=["--no-sandbox"],
        )
        return pw, context
    except Exception:
        await pw.stop()
        raise


async def stop_browser(pw, context):
    try:
        await context.close()
    finally:
        await pw.stop()


async def load(page, url):
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=90000,
    )

    await page.wait_for_timeout(1000)

    if await page.get_by_text(
        "Verify you're human",
        exact=False,
    ).count():
        raise RuntimeError(
            "Website verification required. "
            "Use an authorized browser session."
        )


async def close_popup(page):
    dialog = page.locator(
        '[role="dialog"][aria-modal="true"]'
    )

    if await dialog.count():
        close = dialog.get_by_role(
            "button",
            name="Close",
            exact=True,
        )

        try:
            if await close.count():
                await close.first.click(timeout=4000)
            else:
                await page.keyboard.press("Escape")
        except Exception:
            pass


async def collect_links(page, route):
    return await page.evaluate(
        r"""route => {
            const found = new Map();
            const pattern = new RegExp(
                '^/(' + route + ')/([^/]+)/?$'
            );

            const generic = /^(view classes|view details|view batch|view pack|explore|learn more|open|watch|join|enroll|details|back)\s*[→➜➡]?$/i;

            for (const a of document.querySelectorAll('a[href]')) {
                const u = new URL(a.href);
                if (u.origin !== location.origin) continue;

                const match = u.pathname.match(pattern);
                if (!match) continue;

                const lines = (a.innerText || '')
                    .split('\n')
                    .map(s => s.trim())
                    .filter(Boolean);

                const name = lines.find(x =>
                    x.length >= 4 &&
                    x.length <= 170 &&
                    !generic.test(x)
                ) || '';

                const key = match[1] + ':' + match[2];
                const existing = found.get(key);

                if (!existing || (name && !existing.name)) {
                    found.set(key, {
                        uid: match[2],
                        type: match[1],
                        name: name,
                        url: u.origin + u.pathname,
                    });
                }
            }

            return [...found.values()];
        }""",
        route,
    )


def normalize(rows):
    return [
        {
            "uid": item["uid"],
            "type": item["type"],
            "name": item["name"] or (
                item["type"].title() + " — " + item["uid"]
            ),
            "url": item["url"],
        }
        for item in rows
    ]


# ==================================================
# GOAL DISCOVERY
# ==================================================

async def goals(profile, keyword):
    term = keyword.strip().lower()

    commerce = (
        term == "ca"
        or term.startswith("ca ")
        or term.startswith("cs ")
        or term in {
            "commerce",
            "chartered accountant",
            "chartered accountants",
            "cma",
            "cfa",
            "cs",
            "cseet",
        }
    )

    category_url = BASE + (
        "/?cat=comm&change=1"
        if commerce
        else "/?change=1"
    )

    cache_key = "goal_search:" + term
    state = _read_state(profile)
    cached = state.get(cache_key)

    if (
        cached
        and time.time() - cached.get("time", 0) < 86400
        and cached.get("items")
    ):
        return cached["items"]

    verified = {
        "CA Foundation": "BBKWG",
        "CA Intermediate": "RXRWJ",
        "CA Final Group 1": "WKQWQ",
        "CA Final Group 2": "JUYYX",
    }

    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        await load(page, category_url)

        await page.locator(
            "button.goal-card"
        ).first.wait_for(timeout=30000)

        names = await page.locator(
            "button.goal-card"
        ).evaluate_all(
            """buttons => buttons.map(button => (
                button.querySelector('span.font-semibold')
                    ?.innerText || button.innerText || ''
            ).trim()).filter(Boolean)"""
        )

        if term in {
            "ca",
            "commerce",
            "chartered accountant",
            "chartered accountants",
        }:
            names = [
                name for name in names
                if name.upper().startswith("CA ")
            ]
        else:
            names = [
                name for name in names
                if term in name.lower()
            ]

        names = list(dict.fromkeys(names))
        semaphore = asyncio.Semaphore(3)

        async def discover_uid(name):
            if name in verified:
                return verified[name]

            async with semaphore:
                tab = await context.new_page()

                try:
                    await load(tab, category_url)

                    await tab.locator(
                        "button.goal-card"
                    ).first.wait_for(timeout=20000)

                    await close_popup(tab)

                    target = tab.locator(
                        "button.goal-card"
                    ).filter(
                        has=tab.locator(
                            "span.font-semibold"
                        ).get_by_text(
                            name,
                            exact=True,
                        )
                    ).first

                    await target.click(timeout=15000)

                    await tab.wait_for_url(
                        re.compile(r"/goal/[A-Za-z0-9]+"),
                        timeout=15000,
                    )

                    match = re.search(
                        r"/goal/([A-Za-z0-9]+)",
                        tab.url,
                    )

                    return match.group(1) if match else None

                except Exception as error:
                    print(
                        "Goal skipped:",
                        name,
                        str(error)[:120],
                    )
                    return None

                finally:
                    await tab.close()

        uids = await asyncio.gather(
            *(discover_uid(name) for name in names)
        )

        result = []

        for name, uid in zip(names, uids):
            if uid:
                result.append({
                    "uid": uid,
                    "name": name,
                    "type": "goal",
                    "url": BASE + "/goal/" + uid,
                })

        if result:
            state = _read_state(profile)
            state[cache_key] = {
                "time": time.time(),
                "items": result,
            }
            _write_state(profile, state)

        return result

    finally:
        await stop_browser(pw, context)


# ==================================================
# OPEN PACK / BATCH THROUGH WEBSITE NAVIGATION
# ==================================================

async def _click_from_goal(page, goal_url, kind, uid):
    separator = "&" if "?" in goal_url else "?"
    url = goal_url + separator + "tab=batch"

    await load(page, url)
    await close_popup(page)

    batch_tab = page.get_by_role(
        "button",
        name="Batches",
        exact=True,
    )

    try:
        if await batch_tab.is_visible():
            await batch_tab.click(timeout=4000)
    except Exception:
        pass

    selector = (
        'a[href*="/'
        + kind
        + '/'
        + uid
        + '"]:visible'
    )

    unchanged = 0
    previous = -1

    for _ in range(60):
        link = page.locator(selector).first

        if await link.count():
            await link.click(timeout=15000)

            await page.wait_for_url(
                re.compile(
                    r"/"
                    + re.escape(kind)
                    + r"/"
                    + re.escape(uid)
                ),
                timeout=20000,
            )
            return

        count = await page.locator(
            'a[href*="/pack/"],a[href*="/batch/"]'
        ).count()

        if count == previous:
            unchanged += 1
        else:
            unchanged = 0

        previous = count

        more = page.get_by_role(
            "button",
            name=re.compile(
                r"load more|show more|view more",
                re.I,
            ),
        ).first

        clicked = False

        try:
            if await more.is_visible():
                await more.click(timeout=5000)
                clicked = True
        except Exception:
            pass

        if not clicked:
            await page.evaluate(
                "() => window.scrollTo(0, document.body.scrollHeight)"
            )

        await page.wait_for_timeout(650)

        if unchanged >= 9:
            break

    raise RuntimeError(
        f"{kind.title()} {uid} not found in goal listing"
    )


async def _subject_links(page):
    for _ in range(15):
        rows = normalize(
            await collect_links(page, "batch")
        )

        if rows:
            return rows

        await page.wait_for_timeout(500)

    return []


async def _open_pack(page, pack):
    goal_url = pack.get("parent_goal_url")
    navigation_error = None

    # Click from goal first because direct pack URLs
    # may not initialize website state correctly.
    if goal_url:
        try:
            await _click_from_goal(
                page,
                goal_url,
                "pack",
                pack["uid"],
            )

            await close_popup(page)

            rows = await _subject_links(page)

            if rows:
                return rows

        except Exception as error:
            navigation_error = error

    # Fallback for packs that support direct navigation.
    await load(page, pack["url"])
    await close_popup(page)

    rows = await _subject_links(page)

    if rows:
        return rows

    if navigation_error:
        raise RuntimeError(
            "Pack navigation failed: "
            + str(navigation_error)
        )

    raise RuntimeError(
        "No batch links found in pack "
        + pack["uid"]
        + ". Re-select it from the goal."
    )


# ==================================================
# BATCH / PACK LIST
# ==================================================

async def batches(profile, goal):
    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        await load(
            page,
            goal["url"] + "?tab=batch",
        )

        await close_popup(page)

        tab = page.get_by_role(
            "button",
            name="Batches",
            exact=True,
        )

        try:
            if await tab.is_visible():
                await tab.click(timeout=5000)
        except Exception:
            pass

        found = {}
        stable = 0

        for _ in range(65):
            rows = normalize(
                await collect_links(page, "pack|batch")
            )

            before = len(found)

            for item in rows:
                key = item["type"] + ":" + item["uid"]
                old = found.get(key)

                if (
                    not old
                    or old["name"].startswith(
                        item["type"].title() + " — "
                    )
                ):
                    found[key] = item

            stable = (
                stable + 1
                if len(found) == before
                else 0
            )

            more = page.get_by_role(
                "button",
                name=re.compile(
                    r"load more|show more|view more",
                    re.I,
                ),
            ).first

            clicked = False

            try:
                if await more.is_visible():
                    await more.click(timeout=5000)
                    clicked = True
            except Exception:
                pass

            if not clicked:
                await page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)"
                )

            await page.wait_for_timeout(650)

            if stable >= 5:
                break

        result = list(found.values())

        for item in result:
            item["parent_goal_url"] = goal["url"]

        _remember(profile, result)
        return result

    finally:
        await stop_browser(pw, context)


# ==================================================
# SUBJECT LIST
# ==================================================

async def subjects(profile, pack):
    pack = _restore(profile, pack)

    if pack["type"] == "batch":
        return [pack]

    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        rows = await _open_pack(page, pack)

        for item in rows:
            item["parent_pack_url"] = pack["url"]
            item["parent_pack_uid"] = pack["uid"]
            item["parent_goal_url"] = pack.get(
                "parent_goal_url"
            )

        _remember(profile, rows)

        return rows

    finally:
        await stop_browser(pw, context)


# ==================================================
# CLASS EXTRACTION
# ==================================================

CLASS_JS = r"""
() => {
    const text = document.body.innerText || '';

    const m = text.match(
        /(\d+)\s+classes total\s*[·•]\s*Showing\s+(\d+)/i
    );

    const classes = [];

    for (const a of document.querySelectorAll('a[href]')) {
        if (new URL(a.href).pathname !== '/watch')
            continue;

        let card = a.parentElement;
        let match = null;

        for (let i = 0; i < 12 && card; i++) {
            const t = card.innerText || '';
            match = t.match(/Class\s+(\d+)/i);

            if (match && t.length < 1400)
                break;

            card = card.parentElement;
        }

        if (!match || !card)
            continue;

        const number = Number(match[1]);

        const lines = (card.innerText || '')
            .split('\n')
            .map(x => x.trim())
            .filter(Boolean);

        const title = lines.find(x =>
            !/^Class\s+\d+/i.test(x) &&
            !/^Watch class$/i.test(x) &&
            !/^Download /i.test(x) &&
            x.length > 3
        ) || 'Class ' + number;

        const pdfs = [...card.querySelectorAll('a[href]')]
            .filter(x =>
                new URL(x.href).pathname.startsWith(
                    '/api/public/d/'
                )
            );

        classes.push({
            class_number: number,
            title: title,
            watch_url: a.href,
            annotated_pdf: pdfs.find(
                x => /annotated/i.test(x.innerText)
            )?.href || null,
            plain_pdf: pdfs.find(
                x => /plain/i.test(x.innerText)
            )?.href || null
        });
    }

    return {
        total: m ? Number(m[1]) : null,
        showing: m ? Number(m[2]) : null,
        classes
    };
}
"""


async def _open_batch(page, subject):
    pack_url = subject.get("parent_pack_url")
    goal_url = subject.get("parent_goal_url")

    if pack_url:
        pack = {
            "uid": subject.get("parent_pack_uid")
                or identifier(pack_url),
            "type": "pack",
            "url": pack_url,
            "parent_goal_url": goal_url,
        }

        await _open_pack(page, pack)

        selector = (
            'a[href*="/batch/'
            + subject["uid"]
            + '"]:visible'
        )

        link = page.locator(selector).first

        if not await link.count():
            raise RuntimeError(
                "Batch link missing inside parent pack: "
                + subject["uid"]
            )

        await link.click(timeout=15000)

    elif goal_url:
        await _click_from_goal(
            page,
            goal_url,
            "batch",
            subject["uid"],
        )

    else:
        await load(page, subject["url"])

    await page.wait_for_function(
        """() => {
            const t = document.body.innerText || '';
            return /classes total\\s*[·•]\\s*Showing\\s*\\d+/i.test(t)
                || /Batch not found/i.test(t);
        }""",
        timeout=30000,
    )

    body = await page.locator("body").inner_text()

    if "Batch not found" in body:
        raise RuntimeError(
            "Batch not found: "
            + subject["uid"]
            + ". Parent navigation may be missing."
        )


async def classes(profile, subject):
    subject = _restore(profile, subject)

    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        await _open_batch(page, subject)

        found = {}
        previous_showing = -1
        stagnant = 0

        for _ in range(45):
            result = await page.evaluate(CLASS_JS)

            if result["total"] is None:
                raise RuntimeError(
                    "Class count not found on page."
                )

            for lecture in result["classes"]:
                found[lecture["class_number"]] = lecture

            if result["showing"] >= result["total"]:
                return [
                    found[number]
                    for number in sorted(found)
                ], result["total"]

            if result["showing"] <= previous_showing:
                stagnant += 1
            else:
                stagnant = 0

            previous_showing = result["showing"]

            if stagnant >= 4:
                break

            button = page.get_by_role(
                "button",
                name=re.compile(
                    r"load more classes",
                    re.I,
                ),
            ).first

            try:
                await button.click(timeout=7000)

                await page.wait_for_function(
                    """old => {
                        const t = document.body.innerText || '';
                        const m = t.match(
                            /classes total\\s*[·•]\\s*Showing\\s*(\\d+)/i
                        );
                        return m && Number(m[1]) > old;
                    }""",
                    arg=result["showing"],
                    timeout=15000,
                )

            except Exception:
                await page.wait_for_timeout(1200)

        raise RuntimeError(
            "Incomplete class pagination: "
            + str(len(found))
            + " playable links found."
        )

    finally:
        await stop_browser(pw, context)
