"""Cinema discovery + insertion.

Two CLI modes:

    # Add one cinema by name (pilot mode):
    python -m crawlers.sync_cinemas one-shot \\
        --chain CGV --cinema-code 0059 --name 영등포타임스퀘어

    # Diff a chain's live cinema list against the DB and insert any new ones
    # as commercial_only / is_arthouse_venue=false:
    python -m crawlers.sync_cinemas chain --chain CGV [--region 01] [--limit N] [--dry-run]

Required env:
  SUPABASE_URL, SUPABASE_KEY
  NCP_API_KEY_ID, NCP_API_KEY                       (Naver Maps Geocoding)
  NAVER_SEARCH_CLIENT_ID, NAVER_SEARCH_CLIENT_SECRET (Naver Local Search)
  CGV_SIGN_SECRET                                   (chain CGV only)
"""

from __future__ import annotations

import argparse
import json
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


def sync_cgv(
    *,
    region: str | None = "01",
    limit: int | None = None,
    skip_premium: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Diff CGV's live cinema list (default: Seoul) against the DB and insert
    any new entries as commercial-only / non-arthouse venues.

    Premium sub-brands (씨네드쉐프 등, site codes starting with 'P') are skipped
    by default — they share a building with an existing CGV and aren't a
    distinct discovery target.
    """
    # Imported lazily so callers using `one-shot` don't need CGV_SIGN_SECRET.
    from crawlers.cgv import fetch_cgv_cinema_list

    sites = fetch_cgv_cinema_list(region=region)
    if skip_premium:
        sites = [s for s in sites if not s["siteNo"].startswith("P")]

    supabase = SupabaseClient()
    existing_codes = {c["cinema_code"] for c in supabase.fetch_cinemas(chain="CGV")}

    new_sites = [s for s in sites if s["siteNo"] not in existing_codes]
    to_process = new_sites[:limit] if limit else new_sites

    added: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for s in to_process:
        try:
            r = add_cinema_one_shot(
                "CGV", s["siteNo"], s["siteNm"], dry_run=dry_run
            )
            added.append({"siteNo": s["siteNo"], "siteNm": s["siteNm"], **r})
        except (GeocodingError, Exception) as e:
            errors.append(
                {"siteNo": s["siteNo"], "siteNm": s["siteNm"], "error": str(e)}
            )

    return {
        "chain": "CGV",
        "region": region,
        "discovered": len(sites),
        "existing_in_db": len(sites) - len(new_sites),
        "new": len(new_sites),
        "processed": len(to_process),
        "dry_run": dry_run,
        "added": added,
        "errors": errors,
    }


_CHAIN_SYNC_FNS = {
    "CGV": sync_cgv,
    # Megabox / Lotte will be added in follow-up tasks.
}


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

    chain = sub.add_parser(
        "chain", help="Discover + insert all new cinemas for a chain"
    )
    chain.add_argument("--chain", required=True, choices=list(_CHAIN_SYNC_FNS.keys()))
    chain.add_argument(
        "--region",
        default="01",
        help="Region code to limit discovery (CGV: 01=Seoul, 02=경기, etc.). "
        'Pass "all" to disable filtering.',
    )
    chain.add_argument(
        "--limit", type=int, help="Process at most N new cinemas this run"
    )
    chain.add_argument(
        "--include-premium",
        action="store_true",
        help="Include CGV premium sub-brands (씨네드쉐프, site codes starting "
        "with 'P'). Default: skip.",
    )
    chain.add_argument("--dry-run", action="store_true")

    args = p.parse_args()

    try:
        if args.cmd == "one-shot":
            result = add_cinema_one_shot(
                args.chain, args.cinema_code, args.name, dry_run=args.dry_run
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "chain":
            region = None if args.region == "all" else args.region
            fn = _CHAIN_SYNC_FNS[args.chain]
            result = fn(
                region=region,
                limit=args.limit,
                skip_premium=not args.include_premium,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result.get("errors") else 1
    except GeocodingError as e:
        print(f"Geocoding failed: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(_main())
