import re
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE = "https://unacademy.hrvertex.workers.dev"

KNOWN_GOALS = [
    {
        "uid": "RXRWJ",
        "name": "CA Intermediate",
        "url": BASE + "/goal/RXRWJ",
    }
]


def uid(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


async def open_browser(profile):
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


async def close_browser(pw, context):
    await context.close()
    await pw.stop()


async def navigate(page, url):
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    await page.wait_for_timeout(2500)


async def read_links(page):
    return await page.locator("a[href]").evaluate_all("""
    anchors => anchors.map(a => ({
        name: (a.innerText || a.getAttribute("aria-label") || "")
            .split("\\n").map(x => x.trim()).find(Boolean) || "",
        url: a.href,
        path: new URL(a.href).pathname
    }))
    """)


def filter_links(rows, pattern):
    items = {}

    for row in rows:
        url = row.get("url", "")
        path = row.get("path", "")

        if urlparse(url).netloc != urlparse(BASE).netloc:
            continue

        if not re.fullmatch(pattern, path):
            continue

        items[path] = {
            "uid": uid(url),
            "name": row.get("name") or path,
            "url": BASE + path,
        }

    return list(items.values())


async def discover_goals(keyword, profile):
    pw, context = await open_browser(profile)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await navigate(page, BASE + "/")

        inputs = page.locator(
            'input[type="search"], input[placeholder*="Search" i]'
        )

        if await inputs.count():
            await inputs.first.fill(keyword)
            await page.wait_for_timeout(1800)

        results = filter_links(
            await read_links(page),
            r"/goal/[^/]+",
        )

        for known in KNOWN_GOALS:
            if known["uid"] not in {x["uid"] for x in results}:
                results.append(known)

        term = keyword.strip().lower()

        if term in ("ca", "chartered accountant", "chartered accountants"):
            return [
                x for x in results
                if x["name"].lower().startswith("ca ")
                or "chartered" in x["name"].lower()
            ]

        return [
            x for x in results
            if term in x["name"].lower()
        ]
    finally:
        await close_browser(pw, context)


async def discover_batches(goal, profile):
    pw, context = await open_browser(profile)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await navigate(page, goal["url"] + "?tab=batch")

        found = {}
        unchanged = 0

        for _ in range(20):
            before = len(found)

            for entry in filter_links(
                await read_links(page),
                r"/(?:pack|batch)/[^/]+",
            ):
                found[entry["url"]] = entry

            if len(found) == before:
                unchanged += 1
            else:
                unchanged = 0

            more = page.get_by_role(
                "button",
                name=re.compile(
                    r"load more|show more|view more|next",
                    re.I,
                ),
            ).first

            try:
                if not await more.is_visible():
                    break
                await more.click(timeout=6000)
                await page.wait_for_timeout(1400)
            except Exception:
                break

            if unchanged >= 3:
                break

        return list(found.values())
    finally:
        await close_browser(pw, context)


async def discover_subjects(pack, profile):
    if "/batch/" in pack["url"]:
        return [pack]

    pw, context = await open_browser(profile)
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await navigate(page, pack["url"])

        return filter_links(
            await read_links(page),
            r"/batch/[^/]+",
        )
    finally:
        await close_browser(pw, context)


CLASS_EXTRACTOR = r"""
() => {
    const body = document.body.innerText || "";
    const count = body.match(
        /(\d+)\s+classes total\s*[·•]\s*Showing\s+(\d+)/i
    );

    const classes = [];

    for (const link of document.querySelectorAll("a[href]")) {
        if (new URL(link.href).pathname !== "/watch") continue;

        let node = link.parentElement;
        let card = null;

        for (let depth = 0; depth < 12 && node; depth++, node = node.parentElement) {
            const t = node.innerText || "";

            if (/Class\s+\d+\s*[·•-]/i.test(t) && t.length < 1400) {
                card = node;
                break;
            }
        }

        if (!card) continue;

        const text = card.innerText || "";
        const classMatch = text.match(/Class\s+(\d+)/i);

        if (!classMatch) continue;

        const lines = text.split("\n").map(x => x.trim()).filter(Boolean);

        const title = lines.find(x =>
            !/^Class\s+\d+/i.test(x) &&
            !/^Watch class$/i.test(x) &&
            !/^Download /i.test(x) &&
            !/^\d{1,2}\/\d{1,2}\/\d{4}$/.test(x)
        ) || "Lecture";

        const pdfs = [...card.querySelectorAll("a[href]")]
            .filter(a =>
                new URL(a.href).pathname.startsWith("/api/public/d/")
            );

        classes.push({
            class_number: Number(classMatch[1]),
            title: title,
            watch_url: link.href,
            annotated_pdf: pdfs.find(
                a => /annotated/i.test(a.innerText)
            )?.href || null,
            plain_pdf: pdfs.find(
                a => /plain/i.test(a.innerText)
            )?.href || null
        });
    }

    return {
        total: count ? Number(count[1]) : null,
        showing: count ? Number(count[2]) : null,
        classes
    };
}
"""


async def discover_classes(subject, profile):
    pw, context = await open_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await navigate(page, subject["url"])

        found = {}
        max_showing = 0
        expected = None

        for attempt in range(25):
            result = await page.evaluate(CLASS_EXTRACTOR)

            if result["total"] is None:
                raise RuntimeError(
                    "Batch page did not show a valid class count."
                )

            expected = result["total"]
            max_showing = max(max_showing, result["showing"])

            for lecture in result["classes"]:
                found[lecture["class_number"]] = lecture

            if max_showing >= expected:
                return (
                    [found[k] for k in sorted(found)],
                    expected,
                )

            button = page.get_by_role(
                "button",
                name=re.compile(r"load more classes", re.I),
            ).first

            try:
                if not await button.is_visible():
                    raise RuntimeError(
                        f"Load More missing at {max_showing}/{expected}"
                    )

                await button.click(timeout=6000)
            except Exception:
                await page.wait_for_timeout(1500)

            await page.wait_for_timeout(1400)

        raise RuntimeError(
            f"Incomplete class list: {max_showing}/{expected}. "
            "No upload job started for this subject."
        )

    finally:
        await close_browser(pw, context)
