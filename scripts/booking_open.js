// Phase 39 — open booking links in Chromium and say what each page filled in. Run by scripts/booking_check.py:
//   NODE_PATH=src/frontend/node_modules node scripts/booking_open.js <url> …
// A normal browser's user agent: Booking.com answers headless Chrome's own with a challenge that drops the search.
const { chromium } = require("@playwright/test");

const AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36";

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ locale: "en-IN", timezoneId: "Asia/Kolkata", userAgent: AGENT, viewport: { width: 1280, height: 900 } });
  for (const url of process.argv.slice(2)) {
    const page = await context.newPage();
    try {
      await page.goto(url, { waitUntil: "domcontentloaded", timeout: 45000 });
      await page.waitForTimeout(6000);
      const seen = await page.evaluate(() => {
        const text = document.body.innerText.replace(/\s+/g, " ");
        const fields = [...document.querySelectorAll("input")].filter((i) => i.value && i.type !== "hidden")
          .map((i) => `${i.getAttribute("aria-label") || i.name || "?"}=${i.value}`);
        return {
          title: document.title,
          fields: fields.slice(0, 8),
          dates: (text.match(/Select dates (.{0,40}?) Select occupancy/) || [null, null])[1],
          occupancy: (text.match(/\d+ adults? · \d+ child(ren)? · \d+ rooms?/) || [null])[0],
          trip: (text.match(/(Round trip|One way) \d+ (Economy|Premium economy|Business|First)/) || [null])[0],
          first: (document.querySelector("[data-testid=title]") || {}).innerText || null,
        };
      });
      console.log(JSON.stringify({ url, ...seen }));
    } catch (error) {
      console.log(JSON.stringify({ url, error: String(error).slice(0, 200) }));
    }
    await page.close();
  }
  await browser.close();
})();
