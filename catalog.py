import re
import asyncio
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE = "https://unacademy.hrvertex.workers.dev"
GENERIC = re.compile(
    r"^(view classes|view details|view batch|"
    r"view pack|explore|learn more|open|watch|"
    r"join|enroll|details|back)$",
    re.I,
)


def identifier(url):
    return urlparse(url).path.rstrip("/").split("/")[-1]


async def start_browser(profile):
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
    await context.close()
    await pw.stop()


async def load(page, url):
    await page.goto(
        url,
        wait_until="domcontentloaded",
        timeout=90000,
    )
    await page.wait_for_timeout(3500)

    if await page.get_by_text(
        "Verify you're human", exact=False
    ).count():
        raise RuntimeError(
            "Website verification required. "
            "Open with your authorized browser session."
        )


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
              title.length <= 170 && !generic.test(title);

            const key = match[1] + ':' + match[2];

            // Crucial: a generic action link can never
            // replace a valid title link.
            const existing = found.get(key);
            if (!existing || (valid && !existing.valid)) {
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


async def goals(profile, keyword):
    pw, context = await start_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()

        # This is the actual goal selection page.
        await load(page, BASE + "/?change=1")

        term = keyword.strip()
        ca_search = term.lower() in {
            "ca", "chartered accountant",
            "chartered accountants", "commerce",
        }

        # On the goal selector, categories expand goals.
        # Expand CA category for CA-related searches.
        if ca_search:
            category = page.get_by_text(
                "CA, CS & Commerce", exact=False
            ).first
            try:
                await category.click(timeout=5000)
                await page.wait_for_timeout(1800)
            except Exception:
                pass

        search = page.locator(
            'input[placeholder*="Search for your goal" i]'
        ).first

        try:
            if await search.is_visible():
                await search.fill(
                    "CA" if ca_search else term
                )
                await page.wait_for_timeout(1500)
        except Exception:
            pass

        rows = normalize(
            await collect_links(page, "goal")
        )

        if ca_search:
            return [
                x for x in rows
                if re.search(
                    r"\bCA\b|Chartered Accountant",
                    x["name"],
                    re.I,
                )
            ]

        return [
            x for x in rows
            if term.lower() in x["name"].lower()
        ]

    finally:
        await stop_browser(pw, context)


async def batches(profile, goal):
    pw, context = await start_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await load(page, goal["url"] + "?tab=batch")

        # Some versions default to educators.
        batch_tab = page.get_by_role(
            "button", name="Batches", exact=True
        )

        try:
            if await batch_tab.is_visible():
                await batch_tab.click(timeout=5000)
                await page.wait_for_timeout(1400)
        except Exception:
            pass

        found = {}
        stable = 0

        for _ in range(50):
            entries = normalize(
                await collect_links(page, "pack|batch")
            )

            before = len(found)

            for x in entries:
                key = x["type"] + ":" + x["uid"]

                old = found.get(key)

                if (
                    not old
                    or old["name"].endswith(" — " + x["uid"])
                ):
                    found[key] = x

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


async def subjects(profile, pack):
    if pack["type"] == "batch":
        return [pack]

    pw, context = await start_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await load(page, pack["url"])

        return normalize(
            await collect_links(page, "batch")
        )

    finally:
        await stop_browser(pw, context)


CLASS_JS = r"""
() => {
  const text = document.body.innerText || '';
  const m = text.match(
    /(\d+)\s+classes total\s*[·•]\s*Showing\s+(\d+)/i
  );

  const classes = [];

  for (const a of document.querySelectorAll('a[href]')) {
    if (new URL(a.href).pathname !== '/watch') continue;

    let card = a.parentElement;
    let match = null;

    for (let i = 0; i < 12 && card; i++) {
      const t = card.innerText || '';
      match = t.match(/Class\s+(\d+)/i);

      if (match && t.length < 1400) break;
      card = card.parentElement;
    }

    if (!match || !card) continue;

    const number = Number(match[1]);
    const lines = (card.innerText || '')
      .split('\n').map(x => x.trim()).filter(Boolean);

    const title = lines.find(x =>
      !/^Class\s+\d+/i.test(x) &&
      !/^Watch class$/i.test(x) &&
      !/^Download /i.test(x) &&
      x.length > 3
    ) || 'Class ' + number;

    const pdfs = [...card.querySelectorAll('a[href]')]
      .filter(x => new URL(x.href).pathname.startsWith('/api/public/d/'));

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


async def classes(profile, subject):
    pw, context = await start_browser(profile)

    try:
        page = context.pages[0] if context.pages else await context.new_page()
        await load(page, subject["url"])

        found = {}
        previous_showing = -1
        stagnant = 0

        for _ in range(40):
            result = await page.evaluate(CLASS_JS)

            if result["total"] is None:
                raise RuntimeError(
                    "Class count not found on page."
                )

            for x in result["classes"]:
                found[x["class_number"]] = x

            if result["showing"] >= result["total"]:
                return [
                    found[k] for k in sorted(found)
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
                name=re.compile("load more classes", re.I),
            ).first

            try:
                await button.click(timeout=6000)
            except Exception:
                pass

            await page.wait_for_timeout(1300)

        raise RuntimeError(
            "Incomplete class pagination; refusing to "
            "mark this subject complete."
        )

    finally:
        await stop_browser(pw, context)