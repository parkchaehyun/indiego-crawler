# crawlers/tinyticket.py

import re
import datetime
from typing import List

from playwright.async_api import async_playwright

from crawlers.base import BaseCrawler
from models import Screening, Chain


class TinyTicketCrawler(BaseCrawler):
    chain: Chain = "TinyTicket"
    base_url = "https://www.tinyticket.net/event-manager"

    async def run(self) -> List[Screening]:
        """TinyTicket renders all bookable dates on the event-manager page in
        one go, so we scrape them in a single Playwright session per theater."""
        results: List[Screening] = []
        crawl_ts = datetime.datetime.utcnow().isoformat()

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--single-process",
                    "--disable-dev-shm-usage",
                    "--no-zygote",
                    "--disable-setuid-sandbox",
                    "--disable-accelerated-2d-canvas",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-client-side-phishing-detection",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-domain-reliability",
                    "--disable-features=AudioServiceOutOfProcess",
                    "--disable-hang-monitor",
                    "--disable-ipc-flooding-protection",
                    "--disable-popup-blocking",
                    "--disable-prompt-on-repost",
                    "--disable-renderer-backgrounding",
                    "--disable-sync",
                    "--force-color-profile=srgb",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--no-pings",
                    "--use-gl=swiftshader",
                    "--window-size=1280,1696"
                ]
            )
            page = await browser.new_page()
            await page.set_extra_http_headers({
                'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8'
            })
            await page.add_init_script("Object.defineProperty(navigator, 'language', {get: () => 'ko-KR'})")
            await page.add_init_script("Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko']})")

            for theater in self.theaters:
                url = f"{self.base_url}/{theater.cinema_code}"
                print(f"Processing TinyTicket theater: {theater.name}")

                try:
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    await page.wait_for_selector(".dateLabel", timeout=30000)

                    date_elements = await page.locator(".dateLabel").all()
                    for date_element in date_elements:
                        date_raw = (await date_element.inner_text()).strip()
                        m = re.match(r"(\d{2})/(\d{2})", date_raw)
                        if not m:
                            continue
                        mm, dd = m.groups()
                        play_date = datetime.date(datetime.date.today().year, int(mm), int(dd))

                        card_container = date_element.locator("xpath=following-sibling::div[1]")
                        cards = await card_container.locator(".cardContainer").all()

                        for card in cards:
                            try:
                                box = card.locator(".sq-textbox")
                                if await box.count() == 0:
                                    continue

                                title_spans = box.locator(".nameBox span.nobreak")
                                span_count = await title_spans.count()
                                if span_count < 2:
                                    continue

                                title_text = await title_spans.nth(0).inner_text()
                                title = title_text.replace("radio_button_checked", "").strip()

                                times_raw = (await title_spans.nth(1).inner_text()).replace("schedule", "").strip()
                                if not times_raw or "-" not in times_raw:
                                    continue
                                start_str, end_str = times_raw.split("-", 1)

                                rem_el = box.locator(".salingInfo")
                                if await rem_el.count():
                                    raw_text = await rem_el.inner_text()
                                    txt = raw_text.strip().strip("()")
                                    seat_match = re.search(r'(?:잔여(\d+)|(매진))\s*/\s*(\d+)', txt)
                                    if seat_match:
                                        remaining = int(seat_match.group(1)) if seat_match.group(1) else 0
                                        total = int(seat_match.group(3))
                                    else:
                                        remaining = total = None
                                else:
                                    remaining = total = None

                                results.append(Screening(
                                    provider=self.chain,
                                    cinema_code=theater.cinema_code,
                                    cinema_name=theater.name,
                                    screen_name=theater.name,
                                    movie_title=title,
                                    is_core_art_screen=True,
                                    play_date=play_date.isoformat(),
                                    start_dt=start_str,
                                    end_dt=end_str,
                                    url=url,
                                    remain_seat_cnt=remaining,
                                    total_seat_cnt=total,
                                    crawl_ts=crawl_ts,
                                ))

                            except Exception as e:
                                print(f"Error processing card in {theater.name}: {e}")
                                continue

                except Exception as e:
                    print(f"Error processing theater {theater.name}: {e}")
                    continue

            await browser.close()

        return results
