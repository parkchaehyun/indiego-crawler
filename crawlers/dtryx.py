from __future__ import annotations

import asyncio
import datetime as dt
from typing import Iterable

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_BASE = "https://dtryx.com"
_CGID = "FE8EF4D2-F22D-4802-A39A-D58F23A29C1E"
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Referer": "https://dtryx.com/cinema/movielist.do",
}


class DtryxCrawler(BaseCrawler):
    chain: Chain = "Dtryx"

    async def _fetch_dates(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
    ) -> list[str]:
        """Return ISO YYYY-MM-DD strings for dates this theater has bookings open."""
        brand_cd = theater.brand_cd or "indieart"
        params = {
            "cgid": _CGID,
            "BrandCd": brand_cd,
            "CinemaCd": theater.cinema_code,
            "MovieCd": "all",
            "PlaySDT": "all",
            "_": str(int(dt.datetime.now().timestamp() * 1000)),
        }
        async with sem:
            try:
                resp = await client.get(
                    f"{_BASE}/reserve/main_list.do",
                    params=params,
                    headers={**_HEADERS, "Referer": f"{_BASE}/reserve/movie.do?BrandCd={brand_cd}"},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  ⚠ dates fetch failed for {theater.name}: {type(e).__name__} {repr(e)}")
                return []
        return [
            p["PlaySDT"]
            for p in (data.get("PlaySdtList") or [])
            if p.get("HiddenYn") == "N" and p.get("PlaySDT")
        ]

    async def _fetch_screenings(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
        play_date: str,
    ) -> list[dict]:
        brand_cd = theater.brand_cd or "indieart"
        params = {
            "cgid": _CGID,
            "ssid": "",
            "tokn": "",
            "BrandCd": brand_cd,
            "CinemaCd": theater.cinema_code,
            "PlaySDT": play_date,
            "_": str(int(dt.datetime.now().timestamp() * 1000)),
        }
        async with sem:
            try:
                resp = await client.get(
                    f"{_BASE}/cinema/showseq_list.do",
                    params=params,
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  ⚠ schedule fetch failed for {theater.name} date={play_date}: {type(e).__name__} {repr(e)}")
                return []
        return data.get("Showseqlist") or []

    def _to_screening(
        self, theater: Cinema, item: dict, crawl_ts: str
    ) -> Screening:
        cinema_code = str(item.get("CinemaCd") or "").strip()
        cinema_name = (item.get("CinemaNm") or "").strip()
        is_core_art_screen = cinema_code != "000088" and "아리랑" not in cinema_name
        book_url = (
            f"https://www.dtryx.com/reserve/movie.do"
            f"?cgid={_CGID}"
            f"&CinemaCd={item['CinemaCd']}"
            f"&MovieCd={item['MovieCd']}"
            f"&PlaySDT={item['PlaySDT']}"
            f"&ScreenCd={item['ScreenCd']}"
            f"&ShowSeq={item['ShowSeq']}"
        )
        return Screening(
            provider=self.chain,
            cinema_name=cinema_name,
            cinema_code=cinema_code,
            screen_name=item["ScreenNm"],
            movie_title=item["MovieNmNat"].strip(),
            movie_title_en=(item.get("MovieNmEng") or "").strip() or None,
            source_movie_code=str(item.get("MovieCd") or "").strip() or None,
            is_core_art_screen=is_core_art_screen,
            play_date=item["PlaySDT"],
            start_dt=item["StartTime"],
            end_dt=item["EndTime"],
            crawl_ts=crawl_ts,
            url=book_url,
            remain_seat_cnt=int(item["RemainSeatCnt"]),
            total_seat_cnt=int(item["TotalSeatCnt"]),
        )

    async def run(
        self, start_date: dt.date | None = None, max_days: int | None = None
    ) -> list[Screening]:
        # max_days intentionally ignored: /reserve/main_list.do returns the exact
        # list of dates this theater has bookings open for.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow().isoformat()
        cutoff = start_date.strftime("%Y-%m-%d") if start_date else None

        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=15.0) as client:
            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            date_lists = await asyncio.gather(*[
                self._fetch_dates(client, sem, t) for t in self.theaters
            ])

            jobs: list[tuple[Cinema, str]] = []
            for theater, dates in zip(self.theaters, date_lists):
                effective = [d for d in dates if cutoff is None or d >= cutoff]
                print(
                    f"  {theater.name}: {len(dates)} operational dates "
                    f"({len(effective)} after start_date filter)"
                )
                for d in effective:
                    jobs.append((theater, d))

            if jobs:
                print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs...")
                payloads = await asyncio.gather(*[
                    self._fetch_screenings(client, sem, t, d) for t, d in jobs
                ])
                for (theater, _), items in zip(jobs, payloads):
                    for item in items:
                        try:
                            screenings.append(self._to_screening(theater, item, crawl_ts))
                        except Exception as e:
                            print(f"  ⚠ skip malformed item ({theater.name}): {e}")

        return screenings

    async def iter(self, date: dt.date) -> Iterable[Screening]:
        """Required by BaseCrawler ABC; Dtryx uses its own run() implementation."""
        if False:
            yield  # type: ignore[unreachable]
