from __future__ import annotations

import asyncio
import datetime as dt
import html
import re
from html.parser import HTMLParser
from urllib.parse import urlencode

import httpx

from crawlers.base import BaseCrawler
from models import Cinema, Chain, Screening


_BASE_URL = "https://www.cineq.co.kr"
_SCHEDULE_URL = f"{_BASE_URL}/Theater/MovieTable2"
_DATE_LIST_URL = f"{_BASE_URL}/popup/ReserveDateList"
_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": _BASE_URL,
    "Referer": f"{_BASE_URL}/Theater/Movie?TheaterCode=1001",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}
_KST = dt.timezone(dt.timedelta(hours=9), name="KST")


def _class_tokens(attrs: dict[str, str]) -> set[str]:
    return set((attrs.get("class") or "").split())


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def _normalize_movie_code(raw: str | None) -> str | None:
    code = str(raw or "").strip()
    # CineQ schedule rows append a one-digit screen/property suffix to the
    # eight-digit MovieCode used by Movie/Info and movie-list pages.
    if code.isdigit() and len(code) > 8 and len(set(code[8:])) == 1:
        return code[:8]
    return code or None


def _parse_hhmm_range(text: str) -> tuple[str, str] | None:
    m = re.search(r"(\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})", text)
    if not m:
        return None
    return m.group(1), m.group(2)


def _parse_seats(text: str) -> tuple[int | None, int | None]:
    if "매진" in text:
        return 0, None
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


class _ScheduleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict] = []
        self._stack: list[tuple[str, list[str]]] = []
        self._movie_title: str | None = None
        self._screen_name: str | None = None
        self._time_attrs: dict[str, str] | None = None
        self._title_buf: list[str] = []
        self._screen_buf: list[str] = []
        self._time_buf: list[str] = []
        self._skip_title_depth = 0

    def handle_startendtag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        # Ignore void tags such as <br /> inside title/screen/time blocks; their
        # text-bearing parents remain on the stack.
        return

    def handle_starttag(
        self, tag: str, attrs_list: list[tuple[str, str | None]]
    ) -> None:
        attrs = {k: v or "" for k, v in attrs_list}
        classes = _class_tokens(attrs)
        kinds: list[str] = []

        if tag == "div" and "each-movie-time" in classes:
            self._movie_title = None
            kinds.append("movie")
        if tag == "div" and self._in("movie") and "title" in classes:
            self._title_buf = []
            kinds.append("title")
        if tag == "span" and self._in("title") and any(
            c.startswith("rate-") for c in classes
        ):
            self._skip_title_depth += 1
            kinds.append("rate")
        if tag == "div" and self._in("movie") and "screen" in classes:
            self._screen_name = None
            kinds.append("screen")
        if tag == "div" and self._in("screen") and "screen-name" in classes:
            self._screen_buf = []
            kinds.append("screen_name")
        if tag == "div" and self._in("screen") and "time" in classes:
            self._time_attrs = attrs
            self._time_buf = []
            kinds.append("time")

        self._stack.append((tag, kinds))

    def handle_data(self, data: str) -> None:
        if self._in("title") and self._skip_title_depth == 0:
            self._title_buf.append(data)
        if self._in("screen_name"):
            self._screen_buf.append(data)
        if self._in("time"):
            self._time_buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        open_tag, kinds = self._stack.pop()
        if open_tag != tag:
            return

        if "rate" in kinds and self._skip_title_depth > 0:
            self._skip_title_depth -= 1
        if "title" in kinds:
            self._movie_title = _clean_text("".join(self._title_buf))
            self._title_buf = []
        if "screen_name" in kinds:
            self._screen_name = _clean_text("".join(self._screen_buf))
            self._screen_buf = []
        if "time" in kinds:
            self._finish_time()
        if "screen" in kinds:
            self._screen_name = None
        if "movie" in kinds:
            self._movie_title = None

    def _in(self, kind: str) -> bool:
        return any(kind in kinds for _, kinds in self._stack)

    def _finish_time(self) -> None:
        text = _clean_text(" ".join(self._time_buf))
        time_range = _parse_hhmm_range(text)
        if not time_range or not self._movie_title or not self._time_attrs:
            self._time_attrs = None
            self._time_buf = []
            return

        remain, total = _parse_seats(text)
        self.items.append(
            {
                "play_date": self._time_attrs.get("data-playdate") or "",
                "theater_code": self._time_attrs.get("data-theatercode") or "",
                "movie_code": self._time_attrs.get("data-moviecode") or "",
                "screen_plan_id": self._time_attrs.get("data-screenplanid") or "",
                "play_number": self._time_attrs.get("data-playnumber") or "",
                "movie_title": self._movie_title,
                "screen_name": self._screen_name or "",
                "start_dt": time_range[0],
                "end_dt": time_range[1],
                "remain_seat_cnt": remain,
                "total_seat_cnt": total,
            }
        )
        self._time_attrs = None
        self._time_buf = []


def parse_cineq_schedule(html_text: str) -> list[dict]:
    parser = _ScheduleParser()
    parser.feed(html_text)
    parser.close()
    return parser.items


class CineQCrawler(BaseCrawler):
    chain: Chain = "CineQ"

    async def _post(
        self,
        client: httpx.AsyncClient,
        url: str,
        data: dict[str, str],
    ) -> str | None:
        try:
            resp = await client.post(url, data=data, headers=_HEADERS)
            resp.raise_for_status()
            return resp.text
        except Exception as e:
            print(f"  ⚠ CineQ API call failed ({url} {data}): {e}")
            return None

    async def _fetch_dates(
        self, client: httpx.AsyncClient, theater: Cinema
    ) -> list[str]:
        today = dt.datetime.now(_KST).date()
        today_str = today.strftime("%Y%m%d")
        html_text = await self._post(
            client,
            _DATE_LIST_URL,
            {
                "theaterCode": theater.cinema_code,
                "selectDate": today_str,
                "viewDate": today_str,
            },
        )
        if not html_text:
            return []

        m = re.search(r'data-maxdate="(\d{4}-\d{2}-\d{2})"', html_text)
        if not m:
            return [today.isoformat()]
        max_date = dt.date.fromisoformat(m.group(1))
        if max_date < today:
            return []
        return [
            (today + dt.timedelta(days=i)).isoformat()
            for i in range((max_date - today).days + 1)
        ]

    async def _fetch_screenings(
        self,
        client: httpx.AsyncClient,
        theater: Cinema,
        play_date: str,
    ) -> list[dict]:
        html_text = await self._post(
            client,
            _SCHEDULE_URL,
            {
                "TheaterCode": theater.cinema_code,
                "PlayDate": play_date.replace("-", ""),
            },
        )
        if not html_text:
            return []
        return parse_cineq_schedule(html_text)

    def _to_screening(
        self, theater: Cinema, item: dict, crawl_ts: dt.datetime
    ) -> Screening | None:
        ymd = str(item.get("play_date") or "")
        if len(ymd) != 8:
            return None
        play_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
        screen_plan_id = str(item.get("screen_plan_id") or "").strip()
        movie_code = _normalize_movie_code(item.get("movie_code"))
        params = {
            "playDate": ymd,
            "theaterCode": theater.cinema_code,
        }
        if movie_code:
            params["movieCode"] = movie_code
        if screen_plan_id:
            params["screenPlanId"] = screen_plan_id
        url = f"{_BASE_URL}/?{urlencode(params)}"

        return Screening(
            provider=self.chain,
            cinema_name=theater.name,
            cinema_code=theater.cinema_code,
            screen_name=_clean_text(item.get("screen_name") or ""),
            movie_title=_clean_text(item.get("movie_title") or ""),
            source_movie_code=movie_code,
            is_core_art_screen=False,
            play_date=play_date,
            start_dt=item["start_dt"],
            end_dt=item["end_dt"],
            crawl_ts=crawl_ts.isoformat(),
            url=url,
            remain_seat_cnt=item.get("remain_seat_cnt"),
            total_seat_cnt=item.get("total_seat_cnt"),
        )

    async def run(self) -> list[Screening]:
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow()

        sem = asyncio.Semaphore(4)
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Prime cookies once; the schedule endpoints are simple POSTs but
            # this mirrors the browser flow and keeps the request shape stable.
            await client.get(f"{_BASE_URL}/Theater/Movie?TheaterCode=1001", headers=_HEADERS)

            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            date_lists = await asyncio.gather(*[
                self._fetch_dates(client, t) for t in self.theaters
            ])

            jobs: list[tuple[Cinema, str]] = []
            for theater, dates in zip(self.theaters, date_lists):
                print(f"  {theater.name}: {len(dates)} candidate dates")
                for d in dates:
                    jobs.append((theater, d))

            if jobs:
                print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs...")
                async def fetch_job(theater: Cinema, play_date: str) -> list[dict]:
                    async with sem:
                        return await self._fetch_screenings(client, theater, play_date)

                payloads = await asyncio.gather(*[
                    fetch_job(t, d) for t, d in jobs
                ])
                for (theater, _), items in zip(jobs, payloads):
                    for item in items:
                        try:
                            screening = self._to_screening(theater, item, crawl_ts)
                            if screening is not None:
                                screenings.append(screening)
                        except Exception as e:
                            print(f"  ⚠ skip malformed item ({theater.name}): {e}")

        return screenings
