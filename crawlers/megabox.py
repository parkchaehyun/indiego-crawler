from __future__ import annotations

import asyncio
import datetime as dt
import html
import os
import re

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_URL = "https://www.megabox.co.kr/on/oh/ohc/Brch/schedulePage.do"
_BOOKING_LIST_URL = "https://www.megabox.co.kr/on/oh/ohb/SimpleBooking/selectBokdList.do"

# Megabox bans the egress IP after a burst (~75 requests at ~200 req/s drops
# the connection on the rest). Probed up to 10 req/s sustained with 100% success;
# 5 req/s (200ms) gives 2x headroom. Tune via env if Megabox tightens later.
_MIN_REQUEST_INTERVAL_S = float(os.getenv("MEGABOX_MIN_INTERVAL_S", "0.2"))
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

# Seoul = "10" in Megabox's areaCd taxonomy.
SEOUL_AREA_CODE = "10"


def fetch_megabox_cinema_list(area_code: str | None = SEOUL_AREA_CODE) -> list[dict]:
    """Fetch Megabox's booking cinema list once.

    Returns: list of {'brchNo', 'brchNm', 'areaCd'} dicts. Pass area_code=None
    to get every area; defaults to Seoul ('10').
    """
    play_de = dt.date.today().strftime("%Y%m%d")
    body = {
        "playDe": play_de,
        "incomeMovieNo": "",
        "brchNo": "",
        "areaCd": "",
        "brchNo1": "",
        "movieNo": "",
        "movieNo1": "",
        "onLoad": "Y",
    }
    r = httpx.post(_BOOKING_LIST_URL, json=body, headers=_HEADERS, timeout=15.0)
    if r.status_code != 200:
        raise RuntimeError(
            f"Megabox cinema list HTTP {r.status_code}: {r.text[:200]}"
        )

    data = r.json()
    items = data.get("areaBrchList") or (data.get("megaMap") or {}).get("areaBrchList") or []
    cinemas: list[dict] = []
    seen: set[str] = set()
    for item in items:
        brch_no = str(item.get("brchNo") or "").strip()
        brch_nm = html.unescape(str(item.get("brchNm") or "")).strip()
        row_area = str(item.get("areaCd") or "").strip()
        if not brch_no or not brch_nm:
            continue
        if area_code is not None and row_area != area_code:
            continue
        if brch_no in seen:
            continue
        seen.add(brch_no)
        cinemas.append({"brchNo": brch_no, "brchNm": brch_nm, "areaCd": row_area})
    return cinemas


class _RateLimiter:
    """Enforces a minimum interval between request starts.

    Megabox enforces a per-IP burst threshold (TCP RST after ~75 fast requests).
    A simple inter-arrival gate keeps sustained throughput well below it.
    """

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = asyncio.get_event_loop().time()
            self._next_allowed = now + self._min_interval


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
        limiter: _RateLimiter,
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
        await limiter.acquire()
        try:
            resp = await client.post(_URL, json=body, headers=_HEADERS)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            # httpx.RemoteProtocolError surfaces with empty str(e); include class
            # so the log distinguishes WAF-ban vs timeout vs other failure modes.
            print(f"  ⚠ {theater.name} playDe={play_de} fetch failed: "
                  f"{type(e).__name__}: {e}")
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

    async def run(self) -> list[Screening]:
        # schedulePage with firstAt=Y returns the operational date list, so we
        # crawl exactly Megabox's booking horizon for each theater.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow()
        today_str = dt.date.today().strftime("%Y%m%d")

        # Single shared limiter pacing every request — schedulePage burst at
        # ~200 req/s gets the IP banned mid-batch; 5 req/s sustains cleanly.
        limiter = _RateLimiter(min_interval=_MIN_REQUEST_INTERVAL_S)
        async with httpx.AsyncClient(timeout=20.0) as client:
            # One firstAt=Y call per theater returns both the date list AND today's
            # screenings — reuse the latter to save a call per theater.
            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            first_results = await asyncio.gather(*[
                self._fetch(client, limiter, t, today_str, first=True)
                for t in self.theaters
            ], return_exceptions=True)

            jobs: list[tuple[Cinema, str]] = []
            for theater, data in zip(self.theaters, first_results):
                if isinstance(data, BaseException):
                    print(f"  ⚠ {theater.name}: dates fetch raised "
                          f"{type(data).__name__}: {data}")
                    continue
                if not data:
                    continue
                mm = data.get("megaMap") or {}
                operational = [
                    d.get("playDe")
                    for d in (mm.get("movieFormDeList") or [])
                    if d.get("formAt") == "Y" and d.get("playDe")
                ]
                print(f"  {theater.name}: {len(operational)} operational dates")
                if today_str in operational:
                    screenings.extend(self._items_to_screenings(
                        theater, mm.get("movieFormList") or [], crawl_ts
                    ))
                    remaining = [d for d in operational if d != today_str]
                else:
                    remaining = operational
                for d in remaining:
                    jobs.append((theater, d))

            if jobs:
                print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs...")
                payloads = await asyncio.gather(*[
                    self._fetch(client, limiter, t, d, first=False) for t, d in jobs
                ], return_exceptions=True)
                for (theater, scn_ymd), data in zip(jobs, payloads):
                    if isinstance(data, BaseException):
                        print(f"  ⚠ {theater.name} {scn_ymd}: schedule fetch raised "
                              f"{type(data).__name__}: {data}")
                        continue
                    if not data:
                        continue
                    items = (data.get("megaMap") or {}).get("movieFormList") or []
                    screenings.extend(self._items_to_screenings(theater, items, crawl_ts))

        return screenings
