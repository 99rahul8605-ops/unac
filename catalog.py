
import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE = "https://unacademy.hrvertex.workers.dev"

GENERIC = re.compile(
    r"^(view classes|view details|view batch|view pack|"
    r"explore|learn more|open|watch|join|enroll|details|back)$",
    re.I,
)


def identifier(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


async def start_browser(profile):
    pw = await async_playwright().start()
    try:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile),
            headless=True,
            args=["--no-sandbox"],
        )
        return pw, ctx
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
    await page.wait_for_timeout(2000)

    if await page.get_by_text(
        "Verify you're human", exact=False
    ).count():
        raise RuntimeError("Website verification required")


async def close_popup(page):
    dialog = page.locator(
        '[role="dialog"][aria-modal="true"]'
    )
    if await dialog.count():
        close = dialog.get_by_role(
            "button", name="Close", exact=True
        )
        try:
            if await close.count():
                await close.first.click(timeout=3000)
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

            for (const a of document.querySelectorAll('a[href]')) {
                const u = new URL(a.href);
                if (u.origin !== location.origin) continue;

                const match = u.pathname.match(pattern);
                if (!match) continue;

                const title = (a.innerText || '')
                    .trim().replace(/\s+/g, ' ');

                const generic = /^(view classes|view details|view batch|view pack|explore|learn more|open|watch|join|enroll|details|back)$/i;

                const valid = title.length >= 4 &&
                    title.length <= 170 &&
                    !generic.test(title);

                const key = match[1] + ':' + match[2];
                const old = found.get(key);

                if (!old || (valid && !old.valid)) {
                    found.set(key, {
                        uid: match[2],
                        type: match[1],
                        name: valid ? title : '',
                        url: u.origin + u.pathname,
                        valid: valid
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
            "uid": x["uid"],
            "type": x["type"],
            "name": x["name"] or (
                x["type"].title() + " — " + x["uid"]
            ),
            "url": x["url"],
        }
        for x in rows
    ]


# -------------------------------------------------
# GOALS
# -------------------------------------------------

async def goals(profile, keyword):
    term = keyword.strip().lower()

    is_ca = (
        term == "ca"
        or term.startswith("ca ")
        or term in (
            "commerce",
            "chartered accountant",
            "chartered accountants",
        )
    )

    category_url = BASE + (
        "/?cat=comm&change=1"
        if is_ca else "/?change=1"
    )

    # Verified goal IDs.
    known = {
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

        await page.goto(
            category_url,
            wait_until="domcontentloaded",
            timeout=90000,
        )

        await page.locator(
            "button.goal-card"
        ).first.wait_for(timeout=30000)

        names = await page.locator(
            "button.goal-card"
        ).evaluate_all(
            """buttons => buttons.map(b => (
                b.querySelector('span.font-semibold')
                    ?.innerText || b.innerText || ''
            ).trim()).filter(Boolean)"""
        )

        if is_ca:
            names = [
                n for n in names
                if n.upper().startswith("CA ")
            ]
        else:
            names = [
                n for n in names
                if term in n.lower()
            ]

        result = []

        for name in dict.fromkeys(names):
            uid = known.get(name)

            if not uid:
                try:
                    await page.goto(
                        category_url,
                        wait_until="domcontentloaded",
                        timeout=90000,
                    )

                    await page.locator(
                        "button.goal-card"
                    ).first.wait_for(timeout=20000)

                    await close_popup(page)

                    target = page.locator(
                        "button.goal-card"
                    ).filter(
                        has=page.locator(
                            "span.font-semibold"
                        ).get_by_text(name, exact=True)
                    ).first

                    await target.click(timeout=12000)

                    await page.wait_for_url(
                        re.compile(r".*/goal/[A-Za-z0-9]+.*"),
                        timeout=15000,
                    )

                    match = re.search(
                        r"/goal/([A-Za-z0-9]+)",
                        page.url,
                    )

                    if not match:
                        continue

                    uid = match.group(1)

                except Exception as error:
                    print(
                        "Goal discovery skipped:",
                        name,
                        str(error)[:150],
                    )
                    continue

            result.append({
                "uid": uid,
                "name": name,
                "type": "goal",
                "url": BASE + "/goal/" + uid,
            })

        return result

    finally:
        await stop_browser(pw, context)


# -------------------------------------------------
# BATCHES / PACKS
# -------------------------------------------------

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
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        found = {}
        stable = 0

        for _ in range(50):
            entries = normalize(
                await collect_links(
                    page,
                    "pack|batch",
                )
            )

            before = len(found)

            for item in entries:
                key = (
                    item["type"]
                    + ":"
                    + item["uid"]
                )

                old = found.get(key)

                if (
                    not old
                    or old["name"].endswith(
                        " — " + item["uid"]
                    )
                ):
                    found[key] = item

            if len(found) == before:
                stable += 1
            else:
                stable = 0

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

            await page.wait_for_timeout(900)

            if stable >= 5:
                break

        return list(found.values())

    finally:
        await stop_browser(pw, context)


# -------------------------------------------------
# SUBJECTS - KEEP PARENT PACK CONTEXT
# -------------------------------------------------

async def subjects(profile, pack):
    if pack["type"] == "batch":
        return [pack]

    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        await load(page, pack["url"])

        await close_popup(page)

        rows = await page.evaluate(
            r"""() => {
                const result = new Map();

                for (const a of document.querySelectorAll('a[href]')) {
                    const url = new URL(a.href);
                    const match = url.pathname.match(
                        /^\/batch\/([^/]+)\/?$/
                    );

                    if (!match || url.origin !== location.origin)
                        continue;

                    const lines = (a.innerText || '')
                        .split('\n')
                        .map(x => x.trim())
                        .filter(Boolean);

                    const name = lines.find(x =>
                        x.length > 5 &&
                        x.length < 170 &&
                        !/^(view classes|view details|open|watch)$/i.test(x)
                    ) || 'Batch — ' + match[1];

                    const old = result.get(match[1]);

                    if (!old || old.name.startsWith('Batch — ')) {
                        result.set(match[1], {
                            uid: match[1],
                            type: 'batch',
                            name: name,
                            url: url.origin + url.pathname
                        });
                    }
                }

                return [...result.values()];
            }"""
        )

        for item in rows:
            item["parent_pack_url"] = pack["url"]
            item["parent_pack_uid"] = pack["uid"]

        return rows

    finally:
        await stop_browser(pw, context)


# -------------------------------------------------
# CLASS EXTRACTION
# -------------------------------------------------

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
                new URL(x.href).pathname.startsWith('/api/public/d/')
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


# -------------------------------------------------
# CLASSES - PACK CLICK NAVIGATION
# -------------------------------------------------

async def classes(profile, subject):
    pw, context = await start_browser(profile)

    try:
        page = (
            context.pages[0]
            if context.pages
            else await context.new_page()
        )

        parent = subject.get("parent_pack_url")

        if parent:
            # Direct batch URL does not work reliably.
            # Open the parent pack, then click the subject.
            await load(page, parent)
            await close_popup(page)

            link = page.locator(
                'a[href*="/batch/'
                + subject["uid"]
                + '"]'
            ).first

            if not await link.count():
                raise RuntimeError(
                    "Subject link missing in parent pack: "
                    + subject["uid"]
                )

            await link.click(timeout=15000)

            await page.wait_for_function(
                """() => {
                    const t = document.body.innerText || '';
                    return /classes total\\s*[·•]\\s*Showing\\s+\\d+/i.test(t)
                        || /Batch not found/i.test(t);
                }""",
                timeout=30000,
            )

        else:
            # Some listings have direct batch links only.
            # Keep the original fallback for those cases.
            await load(page, subject["url"])

        text = await page.locator("body").inner_text()

        if "Batch not found" in text:
            raise RuntimeError(
                "Batch not found. Parent pack navigation "
                "may be required for " + subject["uid"]
            )

        found = {}
        previous_showing = -1
        stagnant = 0

        for _ in range(40):
            result = await page.evaluate(CLASS_JS)

            if result["total"] is None:
                raise RuntimeError(
                    "Class count not found on page."
                )

            for item in result["classes"]:
                found[item["class_number"]] = item

            if result["showing"] >= result["total"]:
                return [
                    found[k]
                    for k in sorted(found)
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
                await button.click(timeout=6000)

                await page.wait_for_function(
                    """old => {
                        const text = document.body.innerText || '';
                        const m = text.match(
                            /classes total\\s*[·•]\\s*Showing\\s*(\\d+)/i
                        );
                        return m && Number(m[1]) > old;
                    }""",
                    arg=result["showing"],
                    timeout=12000,
                )

            except Exception:
                await page.wait_for_timeout(1000)

        raise RuntimeError(
            "Incomplete class pagination; refusing "
            "to mark this subject complete."
        )

    finally:
        await stop_browser(pw, context)
