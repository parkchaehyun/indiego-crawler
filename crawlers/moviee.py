from __future__ import annotations

import asyncio
import datetime as dt
import re
from typing import Iterable

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_BASE = "https://moviee.co.kr"
_DATES_URL = f"{_BASE}/api/TicketApi/GetPlayDateList"
_TIMES_URL = f"{_BASE}/api/TicketApi/GetPlayTimeList"
_PROVIDER_ID = "Y24"
_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


class MovieeCrawler(BaseCrawler):
    chain: Chain = "Moviee"

    @staticmethod
    def _to_hhmm(value) -> str | None:
        if value is None:
            return None
        digits = re.sub(r"\D", "", str(value))
        if len(digits) == 3:
            digits = "0" + digits
        if len(digits) != 4:
            return None
        return f"{digits[:2]}:{digits[2:]}"

    @staticmethod
    def _to_int(value) -> int | None:
        if value is None:
            return None
        text = str(value).replace(",", "").strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    async def _fetch_dates(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
    ) -> list[str]:
        params = {
            "tIdList": theater.cinema_code,
            "mId": "",
            "groupCd": -1,
            "mode": 0,
            "gId": "",
            "pId": _PROVIDER_ID,
        }
        async with sem:
            try:
                resp = await client.get(_DATES_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                print(f"  ⚠ dates fetch failed for {theater.name}: {e}")
                return []
        if payload.get("ResCd") != "00":
            print(f"  ⚠ {theater.name} GetPlayDateList ResCd={payload.get('ResCd')}")
            return []
        table = ((payload.get("ResData") or {}).get("Table") or [])
        return [
            (row.get("PLAY_DT") or "").strip()
            for row in table
            if isinstance(row, dict) and (row.get("PLAY_DT") or "").strip()
        ]

    async def _fetch_screenings(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        theater: Cinema,
        play_date: str,
    ) -> list[dict]:
        params = {
            "tId": theater.cinema_code,
            "mId": "",
            "playDt": play_date,
            "ntId": "",
            "gId": "",
        }
        async with sem:
            try:
                resp = await client.get(_TIMES_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as e:
                print(f"  ⚠ schedule fetch failed for {theater.name} date={play_date}: {e}")
                return []
        if payload.get("ResCd") != "00":
            print(f"  ⚠ {theater.name} GetPlayTimeList ResCd={payload.get('ResCd')}")
            return []
        return ((payload.get("ResData") or {}).get("Table") or [])

    def _to_screening(
        self, theater: Cinema, item: dict, target_date: str, crawl_ts: str
    ) -> Screening | None:
        movie_title = (item.get("M_NM") or "").strip()
        if not movie_title:
            return None
        start_dt = self._to_hhmm(item.get("PLAY_TIME"))
        end_dt = self._to_hhmm(item.get("END_TIME"))
        if not start_dt or not end_dt:
            return None

        play_date = (item.get("PLAY_DT") or target_date).strip() or target_date
        cinema_name = (item.get("T_NM") or theater.name).strip()
        cinema_code = str(item.get("T_ID") or theater.cinema_code)
        screen_name = (item.get("TS_NM") or "").strip() or "미지정"

        movie_id = (item.get("M_ID") or "").strip()
        ts_id = (item.get("TS_ID") or "").strip()
        pno = item.get("PNO")
        play_date_compact = play_date.replace("-", "")
        booking_url = None
        if movie_id and cinema_code and ts_id and pno not in (None, ""):
            booking_url = (
                f"{_BASE}/Movie/Ticket"
                f"?gId=&mId={movie_id}&tId={cinema_code}"
                f"&playDate={play_date_compact}&pno={pno}&tsid={ts_id}"
            )

        return Screening(
            provider=self.chain,
            cinema_name=cinema_name,
            cinema_code=cinema_code,
            screen_name=screen_name,
            movie_title=movie_title,
            source_movie_code=movie_id or None,
            is_core_art_screen=True,
            play_date=play_date,
            start_dt=start_dt,
            end_dt=end_dt,
            crawl_ts=crawl_ts,
            url=booking_url,
            remain_seat_cnt=self._to_int(item.get("REMAINSEAT_CNT")),
            total_seat_cnt=self._to_int(item.get("SEAT_CNT")),
        )

    async def run(
        self, start_date: dt.date | None = None, max_days: int | None = None
    ) -> list[Screening]:
        # max_days intentionally ignored: GetPlayDateList returns the exact dates
        # each theater operates on.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow().isoformat()
        cutoff = start_date.isoformat() if start_date else None

        sem = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=10.0, headers=_HEADERS) as client:
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
                for (theater, play_date), items in zip(jobs, payloads):
                    for item in items:
                        try:
                            s = self._to_screening(theater, item, play_date, crawl_ts)
                            if s is not None:
                                screenings.append(s)
                        except Exception as e:
                            print(f"  ⚠ skip malformed item ({theater.name}): {e}")

        return screenings

    async def iter(self, date: dt.date) -> Iterable[Screening]:
        """Required by BaseCrawler ABC; Moviee uses its own run() implementation."""
        if False:
            yield  # type: ignore[unreachable]
