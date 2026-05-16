from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import datetime as dt
import hashlib
import hmac
import os
import random
import time

import httpx
from curl_cffi.requests import AsyncSession

from crawlers.base import BaseCrawler
from models import Screening, Chain, Cinema
from crawlers.supabase_client import SupabaseClient


# HMAC secret extracted from CGV's Next.js bundle (chunks/1453-*.js).
# If signed requests start returning 401 (not 403/Cloudflare), it's been rotated —
# re-extract by grepping the bundle for HmacSHA256, then update CGV_SIGN_SECRET.
_SIGN_SECRET = os.environ["CGV_SIGN_SECRET"].encode()
_API_BASE = "https://api.cgv.co.kr"
_BASE_HEADERS = {
    "accept": "application/json",
    "accept-language": "ko-KR",
    "origin": "https://cgv.co.kr",
    "referer": "https://cgv.co.kr/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


def _sign(path: str, body: str = "") -> dict[str, str]:
    ts = str(int(time.time()))
    msg = f"{ts}|{path}|{body}".encode()
    sig = base64.b64encode(hmac.new(_SIGN_SECRET, msg, hashlib.sha256).digest()).decode()
    return {"x-timestamp": ts, "x-signature": sig}


class _RateLimiter:
    """Enforces a minimum interval between request starts, independent of latency.

    CGV's per-IP limit measured ~5 q/s over a ~15s sliding window before sustained
    429s. We target 2.5 q/s per IP (400ms interval) for ~2x safety margin.
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


@dataclasses.dataclass
class _Worker:
    """One proxy IP (or direct) with its own session, rate limiter, and semaphore.

    CGV's rate limit is per source IP, so each worker enforces its own pacing.
    Multiple workers run in parallel to multiply effective throughput.
    """
    label: str
    session: AsyncSession
    sem: asyncio.Semaphore
    limiter: _RateLimiter


class CGVCrawler(BaseCrawler):
    chain: Chain = "CGV"

    def __init__(self, supabase: SupabaseClient, batch_size: int = 10):
        super().__init__(supabase=supabase, batch_size=batch_size)
        if not self.theaters:
            raise ValueError("No CGV theaters found")

    async def _fetch_proxies(self, want: int) -> list[tuple[str, str]]:
        """Return up to `want` Webshare proxies as (url, label) pairs.

        Returns empty list if no API key or fetch fails — caller falls back to direct.
        """
        api_key = os.getenv("WEBSHARE_API_KEY")
        if not api_key:
            return []
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://proxy.webshare.io/api/v2/proxy/list/",
                    params={"mode": "direct", "page_size": 100},
                    headers={"Authorization": f"Token {api_key}"},
                )
                resp.raise_for_status()
            valid = [p for p in resp.json()["results"] if p.get("valid")]
            if not valid:
                raise ValueError("no valid proxies in list")
            chosen = random.sample(valid, k=min(want, len(valid)))
            return [
                (
                    f"http://{p['username']}:{p['password']}@{p['proxy_address']}:{p['port']}",
                    f"{p['proxy_address']}:{p['port']}",
                )
                for p in chosen
            ]
        except Exception as e:
            print(f"  ⚠ Webshare proxy fetch failed: {e}. Proceeding without proxy.")
            return []

    async def _get_signed(
        self,
        worker: _Worker,
        path: str,
        params: dict,
    ) -> dict:
        url = f"{_API_BASE}{path}"
        # Retry on 429 with backoff long enough to clear CGV's sliding window
        # (~15s). Re-sign each attempt so x-timestamp stays current.
        delays = (5.0, 15.0, 30.0)
        last_status = None
        last_body = ""
        for attempt in range(len(delays) + 1):
            await worker.limiter.acquire()
            headers = {**_BASE_HEADERS, **_sign(path)}
            async with worker.sem:
                r = await worker.session.get(
                    url, params=params, headers=headers, timeout=15
                )
            if r.status_code == 200:
                return r.json()
            last_status, last_body = r.status_code, r.text[:200]
            if r.status_code != 429 or attempt == len(delays):
                break
            await asyncio.sleep(delays[attempt])
        raise RuntimeError(
            f"CGV API {path} [{worker.label}] returned {last_status} after retries: {last_body}"
        )

    async def _fetch_dates(self, worker: _Worker, site_no: str) -> list[str]:
        try:
            data = await self._get_signed(
                worker,
                "/cnm/atkt/searchSiteScnscYmdListBySite",
                {"coCd": "A420", "siteNo": site_no},
            )
        except Exception as e:
            print(f"  ⚠ dates fetch failed for siteNo={site_no}: {e}")
            return []
        rows = data.get("data") or []
        return [r["scnYmd"] for r in rows if r.get("scnYmd")]

    async def _fetch_screenings(
        self, worker: _Worker, site_no: str, scn_ymd: str
    ) -> list[dict]:
        try:
            data = await self._get_signed(
                worker,
                "/cnm/atkt/searchMovScnInfo",
                {"coCd": "A420", "siteNo": site_no, "scnYmd": scn_ymd, "rtctlScopCd": "08"},
            )
        except Exception as e:
            print(f"  ⚠ schedule fetch failed for siteNo={site_no} date={scn_ymd}: {e}")
            return []
        return data.get("data") or []

    def _to_screening(
        self, theater: Cinema, item: dict, crawl_ts: dt.datetime
    ) -> Screening:
        scn_ymd = item["scnYmd"]
        theater_name_param = theater.name.replace("CGV", "").strip()
        url = (
            "https://cgv.co.kr/cnm/movieBook/movie?"
            f'movNo={item["movNo"]}&scnYmd={scn_ymd}&siteNo={item["siteNo"]}&'
            f'siteNm={theater_name_param}&scnsNo={item["scnsNo"]}&scnSseq={item["scnSseq"]}'
        )
        return Screening(
            provider=self.chain,
            cinema_name=theater.name,
            # Use the configured theater code as canonical key.
            # CGV's payload `siteNo` can differ from `cinema_code` and break joins.
            cinema_code=theater.cinema_code,
            screen_name=item["scnsNm"],
            movie_title=item["movNm"],
            movie_title_en=(item.get("movEnm") or "").strip() or None,
            source_movie_code=str(item.get("movNo") or "").strip() or None,
            is_core_art_screen=item.get("sascnsGradNm") == "아트하우스",
            start_dt=f'{item["scnsrtTm"][:2]}:{item["scnsrtTm"][2:]}',
            end_dt=f'{item["scnendTm"][:2]}:{item["scnendTm"][2:]}',
            play_date=f"{scn_ymd[:4]}-{scn_ymd[4:6]}-{scn_ymd[6:]}",
            crawl_ts=crawl_ts.isoformat(),
            url=url,
            remain_seat_cnt=int(item["frSeatCnt"]),
            total_seat_cnt=int(item["stcnt"]),
        )

    async def run(self) -> list[Screening]:
        # The dates endpoint tells us exactly which days CGV has booking open.
        screenings: list[Screening] = []
        crawl_ts = dt.datetime.utcnow()

        # Each proxy IP gives ~2.5 q/s sustained, so total throughput scales with N.
        # Default of 4 keeps headroom in a 10-proxy Webshare pool; tune via env var.
        proxy_count = int(os.getenv("CGV_PROXY_COUNT", "4"))
        proxies = await self._fetch_proxies(proxy_count) if proxy_count > 0 else []

        async with contextlib.AsyncExitStack() as stack:
            workers: list[_Worker] = []
            if proxies:
                for url, label in proxies:
                    session = await stack.enter_async_context(
                        AsyncSession(impersonate="chrome124", max_clients=4, proxy=url)
                    )
                    workers.append(_Worker(
                        label=label,
                        session=session,
                        sem=asyncio.Semaphore(2),
                        limiter=_RateLimiter(min_interval=0.4),
                    ))
                print(f"  Using {len(workers)} Webshare proxies in parallel.")
            else:
                session = await stack.enter_async_context(
                    AsyncSession(impersonate="chrome124", max_clients=4)
                )
                workers.append(_Worker(
                    label="direct",
                    session=session,
                    sem=asyncio.Semaphore(2),
                    limiter=_RateLimiter(min_interval=0.4),
                ))
                print("  No proxies — using direct connection.")

            def worker_for(i: int) -> _Worker:
                return workers[i % len(workers)]

            print(f"  Fetching operational dates for {len(self.theaters)} theaters...")
            date_lists = await asyncio.gather(*[
                self._fetch_dates(worker_for(i), t.cinema_code)
                for i, t in enumerate(self.theaters)
            ])

            jobs: list[tuple[Cinema, str]] = []
            for theater, dates in zip(self.theaters, date_lists):
                if not dates:
                    print(f"  {theater.name}: no operational dates (skipping)")
                    continue
                print(
                    f"  {theater.name}: {len(dates)} dates "
                    f"({dates[0]}…{dates[-1]})"
                )
                for d in dates:
                    jobs.append((theater, d))

            if not jobs:
                return []

            print(f"  Fetching schedules for {len(jobs)} (theater × date) pairs across {len(workers)} worker(s)...")
            payloads = await asyncio.gather(*[
                self._fetch_screenings(worker_for(i), t.cinema_code, d)
                for i, (t, d) in enumerate(jobs)
            ])

        seen: set[tuple] = set()
        per_theater: dict[str, int] = {}
        for (theater, _), items in zip(jobs, payloads):
            for item in items:
                key = (
                    item.get("siteNo"),
                    item.get("movNo"),
                    item.get("scnYmd"),
                    item.get("scnsNo"),
                    item.get("scnSseq"),
                    item.get("scnsrtTm"),
                )
                if key in seen:
                    continue
                seen.add(key)
                try:
                    screenings.append(self._to_screening(theater, item, crawl_ts))
                    per_theater[theater.name] = per_theater.get(theater.name, 0) + 1
                except Exception as e:
                    print(f"  ⚠ skip malformed item ({theater.name}): {e}")

        for name, n in per_theater.items():
            print(f"  {name}: {n} screenings")
        return screenings

