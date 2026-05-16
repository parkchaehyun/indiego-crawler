"""Cinema discovery + insertion.

Currently supports a one-shot mode for piloting a single new cinema:

    python -m crawlers.sync_cinemas one-shot \\
        --chain CGV --cinema-code 0059 --name 영등포타임스퀘어

Chain-wide auto-discovery (`--chain CGV` without --cinema-code) will be added in
a follow-up — it fetches each chain's full theater list, diffs against the DB,
and inserts new entries.

Required env:
  SUPABASE_URL, SUPABASE_KEY
  NCP_API_KEY_ID, NCP_API_KEY                       (Naver Maps Geocoding)
  NAVER_SEARCH_CLIENT_ID, NAVER_SEARCH_CLIENT_SECRET (Naver Local Search)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, get_args

from models import Chain
from crawlers.geocoder import GeocodingError, lookup_place_kr
from crawlers.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

# Korean prefix used to anchor geocode queries for each chain.
_CHAIN_QUERY_PREFIX: dict[str, str] = {
    "CGV": "CGV ",
    "Megabox": "메가박스 ",
    "Lotte": "롯데시네마 ",
}


def add_cinema_one_shot(
    chain: str,
    cinema_code: str,
    name: str,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Geocode a single cinema by name and insert it as a commercial-only,
    non-arthouse venue. Skips if a row with the same (chain, cinema_code)
    already exists.
    """
    if chain not in get_args(Chain):
        raise ValueError(f"Unknown chain: {chain}")

    prefix = _CHAIN_QUERY_PREFIX.get(chain, "")
    query = f"{prefix}{name}".strip()

    supabase = SupabaseClient()
    existing = (
        supabase.client.table("cinemas")
        .select("cinema_code,name,chain,latitude,longitude,is_arthouse_venue")
        .eq("chain", chain)
        .eq("cinema_code", cinema_code)
        .execute()
        .data
    )
    if existing:
        logger.info("Skip: %s/%s already in DB: %s", chain, cinema_code, existing[0])
        return {"status": "skipped_exists", "row": existing[0]}

    logger.info("Looking up place %r ...", query)
    lat, lng, road_address = lookup_place_kr(query)
    logger.info("  -> lat=%s lng=%s (via %r)", lat, lng, road_address)

    # Canonical display name follows the existing convention ("CGV용산아이파크몰"
    # — chain prefix concatenated with site name, no space).
    display_name = f"{chain}{name}" if chain == "CGV" else f"{prefix.strip()} {name}"

    payload = {
        "cinema_code": cinema_code,
        "name": display_name,
        "chain": chain,
        "latitude": lat,
        "longitude": lng,
        "programming_mode": "commercial_only",
        "is_arthouse_venue": False,
    }
    if dry_run:
        logger.info("DRY RUN: would insert %s", payload)
        return {"status": "dry_run", "payload": payload}

    supabase.client.table("cinemas").insert(payload).execute()
    logger.info("Inserted %s/%s", chain, cinema_code)
    return {"status": "inserted", "payload": payload}


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Cinema sync / one-shot insertion")
    sub = p.add_subparsers(dest="cmd", required=True)

    one = sub.add_parser("one-shot", help="Insert a single cinema by name")
    one.add_argument("--chain", required=True, choices=list(get_args(Chain)))
    one.add_argument("--cinema-code", required=True)
    one.add_argument(
        "--name",
        required=True,
        help='Venue name as the chain refers to it (e.g. "영등포타임스퀘어"); '
        "the chain prefix is added automatically for the geocoder query.",
    )
    one.add_argument("--dry-run", action="store_true")

    args = p.parse_args()

    try:
        if args.cmd == "one-shot":
            result = add_cinema_one_shot(
                args.chain, args.cinema_code, args.name, dry_run=args.dry_run
            )
            print(result)
            return 0
    except GeocodingError as e:
        print(f"Geocoding failed: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(_main())
