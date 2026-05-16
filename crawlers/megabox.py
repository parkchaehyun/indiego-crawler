from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
from typing import Iterable

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_URL = "https://www.megabox.co.kr/on/oh/ohc/Brch/schedulePage.do"
_HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.megabox.co.kr",
    "Referer": "https://www.megabox.co.kr/booking/timetable",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class MegaboxCrawler(BaseCrawler):
    chain: Chain = "Megabox"

    @staticmethod
    def _normalize_screen_name(raw_name: str) -> str:
        name = html.unescape(raw_name or "").strip()
        # Remove trailing format/tech suffixes only.
        name = re.sub(r"\s*\[[^\]]*]\s*$", "", name)
        name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
        return re.sub(r"\s+", " ", name).strip()

    async def _fetch(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
        play_de: str,
        first: bool,
    ) -> dict | None:
        body = {
            "masterType": "brch",
            "detailType": "area",
            "brchNo": theater.cinema_code,
            "brchNo1": theater.cinema_code,
            "firstAt": "Y" if first else "N",
            "crtDe": dt.date.today().strftime("%Y%m%d"),
            "playDe": play_de,
        }
        async with sem:
            try:
                resp = await client.post(_URL, json=body, headers=_HEADERS)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                print(f"  ⚠ {theater.name} playDe={play_de} fetch failed: {e}")
                return None

    def _items_to_screenings(
        self, theater: Cinema, items: list[dict], crawl_ts: dt.datetime
    ) -> list[Screening]:
        out: list[Screening] = []
        for item in items:
            try:
                cinema_name = html.unescape(item["brchNm"]).strip()
                screen_name = self._normalize_screen_name(item.get("theabExpoNm"))
                branch_code = str(item.get("brchNo") or "").strip()
                is_core_art_screen = (
                    (cinema_name == "코엑스" and screen_name in {"스크린A", "스크린B"})
                    or branch_code == "0081"
                    or "픽쳐하우스" in cinema_name
                )
                play_schdl_no = item.get("playSchdlNo")
                book_url = (
                    f"https://www.megabox.co.kr/bookingByPlaySchdlNo?playSchdlNo={play_schdl_no}"
                    if play_schdl_no
                    else None
                )
                play_de = str(item.get("playDe") or "")
                play_date = f"{play_de[:4]}-{play_de[4:6]}-{play_de[6:]}" if len(play_de) == 8 else ""

                out.append(Screening(
                    provider=self.chain,
                    cinema_name=cinema_name,
                    cinema_code=branch_code,
                    screen_name=screen_name,
                    movie_title=html.unescape(item["rpstMovieNm"]).strip(),
                    movie_title_en=html.unescape(item.get("movieEngNm") or "").strip() or None,
                    source_movie_code=str(
                        item.get("rpstMovieNo") or item.get("movieNo") or ""
                    ).strip() or None,
                    is_core_art_screen=is_core_art_screen,
                    play_date=play_date,
                    start_dt=item["playStartTime"],
                    end_dt=item["playEndTime"],
                    crawl_ts=crawl_ts.isoformat(),
                    url=book_url,
                    remain_seat_cnt=int(item["restSeatCnt"]),
                    total_seat_cnt=int(item["totSeatCnt"]),
                ))
            except Exception as e:
                print(f"  ⚠ skip malformed item ({theater.name}): {e}")
        return out

    async def run(
        self, start_date: dt.date | None = None, max_days: int | None = None
    ) -> list[Screening]:
        # max_days intentionally ignored: schedulePage with firstAt=Y returns the
        # operational date list, so we crawl exactly Megabox's booking horizon.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow()
        today_str = dt.date.today().strftime("%Y%m%d")
        cutoff = start_date.strftime("%Y%m%d") if start_date else None

        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=15.0) as client:
            # One firstAt=Y call per theater returns both the date list AND today's
            # screenings — reuse the latter if today falls within our crawl window.
            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            first_results = await asyncio.gather(*[
                self._fetch(client, sem, t, today_str, first=True) for t in self.theaters
            ])

            jobs: list[tuple[Cinema, str]] = []
            for theater, data in zip(self.theaters, first_results):
                if not data:
                    continue
                mm = data.get("megaMap") or {}
                operational = [
                    d.get("playDe")
                    for d in (mm.get("movieFormDeList") or [])
                    if d.get("formAt") == "Y" and d.get("playDe")
                ]
                effective = [d for d in operational if cutoff is None or d >= cutoff]
                print(
                    f"  {theater.name}: {len(operational)} operational dates "
                    f"({len(effective)} after start_date filter)"
                )
                if today_str in effective:
                    screenings.extend(self._items_to_screenings(
                        theater, mm.get("movieFormList") or [], crawl_ts
                    ))
                    remaining = [d for d in effective if d != today_str]
                else:
                    remaining = effective
                for d in remaining:
                    jobs.append((theater, d))

            if jobs:
                print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs...")
                payloads = await asyncio.gather(*[
                    self._fetch(client, sem, t, d, first=False) for t, d in jobs
                ])
                for (theater, _), data in zip(jobs, payloads):
                    if not data:
                        continue
                    items = (data.get("megaMap") or {}).get("movieFormList") or []
                    screenings.extend(self._items_to_screenings(theater, items, crawl_ts))

        return screenings

    async def iter(self, date: dt.date) -> Iterable[Screening]:
        """Required by BaseCrawler ABC; Megabox uses its own run() implementation."""
        if False:
            yield  # type: ignore[unreachable]
