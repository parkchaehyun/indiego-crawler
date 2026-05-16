from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_URL = "https://www.lottecinema.co.kr/LCWS/Ticketing/TicketingData.aspx"
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": "https://www.lottecinema.co.kr",
    "Origin": "https://www.lottecinema.co.kr",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class LotteCinemaCrawler(BaseCrawler):
    chain: Chain = "Lotte"

    async def _post(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        param: dict,
    ) -> dict | None:
        async with sem:
            try:
                resp = await client.post(
                    _URL,
                    data={"ParamList": json.dumps(param)},
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                print(f"  ⚠ Lotte API call failed ({param.get('MethodName')}): {e}")
                return None

    async def _fetch_dates(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
    ) -> list[str]:
        """Return ISO YYYY-MM-DD strings for dates this theater operates on."""
        data = await self._post(client, sem, {
            "MethodName": "GetInvisibleMoviePlayInfo",
            "channelType": "HO",
            "osType": "W",
            "osVersion": "Chrome",
            "cinemaList": theater.cinema_code,
            "movieCd": "",
            "playDt": dt.date.today().strftime("%Y-%m-%d"),
        })
        if not data:
            return []
        items = ((data.get("PlayDates") or {}).get("Items")) or []
        # PlayDate looks like "2026-05-16 오전 12:00:00"; keep just the date part.
        return [p["PlayDate"][:10] for p in items if p.get("PlayDate")]

    async def _fetch_screenings(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
        play_date: str,
    ) -> list[dict]:
        data = await self._post(client, sem, {
            "MethodName": "GetPlaySequence",
            "channelType": "HO",
            "osType": "W",
            "osVersion": "Chrome",
            "playDate": play_date,
            "cinemaID": theater.cinema_code,
            "representationMovieCode": "",
        })
        if not data:
            return []
        return ((data.get("PlaySeqs") or {}).get("Items")) or []

    def _to_screening(
        self, theater: Cinema, item: dict, crawl_ts: dt.datetime
    ) -> Screening | None:
        if not item.get("StartTime"):
            return None
        is_core_art_screen = "아르떼" in (item.get("ScreenDivisionNameKR") or "")
        screen_id = item.get("ScreenID")
        cinema_id = item.get("CinemaID")
        movie_cd = item.get("RepresentationMovieCode")
        play_date = item.get("PlayDt")
        start_time = item.get("StartTime")
        book_url = (
            f"https://www.lottecinema.co.kr/NLCHS/ticketing"
            f"?link_screenId={screen_id}"
            f"&link_cinemaCode={cinema_id}"
            f"&link_movieCd={movie_cd}"
            f"&link_date={play_date}"
            f"&link_time={start_time}"
            f"&link_channelCode=naver"
        )
        return Screening(
            provider=self.chain,
            cinema_name=item["CinemaNameKR"],
            cinema_code=theater.cinema_code,
            screen_name=item["ScreenNameKR"],
            movie_title=item["MovieNameKR"].strip(),
            movie_title_en=(item.get("MovieNameUS") or "").strip() or None,
            source_movie_code=str(
                item.get("RepresentationMovieCode") or item.get("MovieCode") or ""
            ).strip() or None,
            is_core_art_screen=is_core_art_screen,
            play_date=play_date,
            start_dt=start_time,
            end_dt=item.get("EndTime"),
            crawl_ts=crawl_ts.isoformat(),
            url=book_url,
            remain_seat_cnt=int(item["BookingSeatCount"]),
            total_seat_cnt=int(item["TotalSeatCount"]),
        )

    async def run(self) -> list[Screening]:
        # GetInvisibleMoviePlayInfo returns the exact list of dates each theater
        # operates on.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow()

        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=15.0) as client:
            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            date_lists = await asyncio.gather(*[
                self._fetch_dates(client, sem, t) for t in self.theaters
            ])

            jobs: list[tuple[Cinema, str]] = []
            for theater, dates in zip(self.theaters, date_lists):
                print(f"  {theater.name}: {len(dates)} operational dates")
                for d in dates:
                    jobs.append((theater, d))

            if jobs:
                print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs...")
                payloads = await asyncio.gather(*[
                    self._fetch_screenings(client, sem, t, d) for t, d in jobs
                ])
                for (theater, _), items in zip(jobs, payloads):
                    for item in items:
                        try:
                            s = self._to_screening(theater, item, crawl_ts)
                            if s is not None:
                                screenings.append(s)
                        except Exception as e:
                            print(f"  ⚠ skip malformed item ({theater.name}): {e}")

        return screenings

