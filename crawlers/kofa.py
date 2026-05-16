# crawlers/kofa.py

import calendar
import datetime as dt
import os

import httpx

from crawlers.base import BaseCrawler
from models import Chain, Screening


class KOFACrawler(BaseCrawler):
    chain: Chain = "KOFA"
    api_url = "https://www.kmdb.or.kr/info/api/3/api.json"
    service_key = os.getenv("KOFA_SERVICE_KEY")

    async def run(self) -> list[Screening]:
        """
        Fetch all screenings from today through the end of the *next* calendar month.
        """
        start = dt.date.today()

        # compute the first day of the month *after* start.month
        year, month = start.year, start.month
        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1

        # last day of that next month
        last_day = calendar.monthrange(next_year, next_month)[1]
        end = dt.date(next_year, next_month, last_day)

        params = {
            "serviceKey": self.service_key,
            "StartDate":  start.strftime("%Y%m%d"),
            "EndDate":    end.strftime("%Y%m%d"),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(self.api_url, params=params)
            resp.raise_for_status()
            data = resp.json()

        programs = data.get("resultList", [])
        results = []
        for item in programs:
            play_date = dt.datetime.strptime(item["cMovieDate"], "%Y%m%d").date()
            # filter just in case
            if play_date < start or play_date > end:
                continue

            run_min    = int(item.get("cRunningTime") or 0)
            start_time = dt.datetime.strptime(item["cMovieTime"], "%H:%M").time()
            start_dt   = dt.datetime.combine(play_date, start_time)
            end_dt     = (start_dt + dt.timedelta(minutes=run_min)).time().strftime("%H:%M")

            raw = item.get("cCodeSubName3") or ""
            if "관" in raw:
                screen_name = raw.split()[-1]
            else:
                screen_name = "Main"

            source_year = None
            raw_year = (item.get("cProductionYear") or "").strip()
            if raw_year.isdigit():
                source_year = int(raw_year)

            results.append(
                Screening(
                    provider     = self.chain,
                    cinema_name  = "시네마테크KOFA",
                    cinema_code  = "KOFA",
                    screen_name  = screen_name,
                    movie_title  = item["cMovieName"].strip(),
                    movie_title_en = (item.get("cMovieNameEng") or "").strip() or None,
                    source_movie_code = (item.get("cMovieId") or "").strip() or None,
                    source_year = source_year,
                    source_director = (item.get("cDirector") or "").strip() or None,
                    is_core_art_screen = True,
                    play_date    = play_date.isoformat(),
                    start_dt     = item["cMovieTime"],
                    end_dt       = end_dt,
                    crawl_ts     = dt.datetime.utcnow().isoformat(),
                    url          = item["homePageURL"]
                )
            )

        return results
