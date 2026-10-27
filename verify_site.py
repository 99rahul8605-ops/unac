# Open a lecture page in a VISIBLE browser using the uploader's profile,
# so the "Verify you're human" check can be completed once by hand.
import asyncio
import sys

sys.path.insert(0, "/root/unac")
from playwright.async_api import async_playwright
from watch_to_api import PROFILE


async def main(url):
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=False,
            args=["--no-sandbox"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, timeout=90000)
        input("Verify karke video page khulne ke baad yahan Enter dabao: ")
        await ctx.close()
        print("Saved. Ab upload dobara chalao.")


asyncio.run(main(sys.argv[1]))
