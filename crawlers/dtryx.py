from crawlers.base import BaseCrawler
from models import Screening, Chain
import httpx
import datetime as dt
from typing import Iterable

class DtryxCrawler(BaseCrawler):
    chain: Chain = "Dtryx"

    async def iter(self, date: dt.date) -> Iterable[Screening]:
        url = "https://dtryx.com/cinema/showseq_list.do"
        crawl_ts = dt.datetime.utcnow().isoformat()

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Referer": "https://dtryx.com/cinema/movielist.do",
        }

        for theater in self.theaters:
            brand_cd = theater.brand_cd or "indieart"
            params = {
                "cgid": "FE8EF4D2-F22D-4802-A39A-D58F23A29C1E",
                "ssid": "",
                "tokn": "",
                "BrandCd": brand_cd,
                "CinemaCd": theater.cinema_code,
                "PlaySDT": date.isoformat(),
                "_": str(int(dt.datetime.now().timestamp() * 1000))
            }

            async with httpx.AsyncClient(timeout=10.0) as client:
                try:
                    resp = await client.get(url, params=params, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    print(f"[{theater.cinema_code}] API request failed: {e}")
                    continue

                for item in data.get("Showseqlist", []):
                    cinema_code = str(item.get("CinemaCd") or "").strip()
                    cinema_name = (item.get("CinemaNm") or "").strip()
                    is_core_art_screen = cinema_code != "000088" and "아리랑" not in cinema_name
                    book_url = (
                        f"https://www.dtryx.com/reserve/movie.do"
                        f"?cgid=FE8EF4D2-F22D-4802-A39A-D58F23A29C1E"
                        f"&CinemaCd={item['CinemaCd']}"
                        f"&MovieCd={item['MovieCd']}"
                        f"&PlaySDT={item['PlaySDT']}"
                        f"&ScreenCd={item['ScreenCd']}"
                        f"&ShowSeq={item['ShowSeq']}"
                    )

                    yield Screening(
                        provider=self.chain,
                        cinema_name=cinema_name,
                        cinema_code=cinema_code,
                        screen_name=item["ScreenNm"],
                        movie_title=item["MovieNmNat"].strip(),
                        movie_title_en=(item.get("MovieNmEng") or "").strip() or None,
                        source_movie_code=str(item.get("MovieCd") or "").strip() or None,
                        is_core_art_screen=is_core_art_screen,
                        play_date=date.isoformat(),
                        start_dt=item["StartTime"],
                        end_dt=item["EndTime"],
                        crawl_ts=crawl_ts,
                        url=book_url,
                        remain_seat_cnt=int(item["RemainSeatCnt"]),
                        total_seat_cnt=int(item["TotalSeatCnt"])
                    )
