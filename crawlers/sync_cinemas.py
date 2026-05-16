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


def _display_name(chain: str, name: str) -> str:
    # Canonical CGV names in this DB are concatenated ("CGV용산아이파크몰").
    if chain == "CGV":
        return f"{chain}{name}"
    prefix = _CHAIN_QUERY_PREFIX.get(chain, "").strip()
    return f"{prefix} {name}".strip() if prefix else name


def _fetch_existing_cinema(
    supabase: SupabaseClient, chain: str, cinema_code: str
) -> dict[str, Any] | None:
    existing = (
        supabase.client.table("cinemas")
        .select("cinema_code,name,chain,latitude,longitude,is_arthouse_venue")
        .eq("chain", chain)
        .eq("cinema_code", cinema_code)
        .execute()
        .data
    )
    return existing[0] if existing else None


def _insert_commercial_cinema(
    supabase: SupabaseClient,
    chain: str,
    cinema_code: str,
    name: str,
    latitude: float,
    longitude: float,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    existing = _fetch_existing_cinema(supabase, chain, cinema_code)
    if existing:
        logger.info("Skip: %s/%s already in DB: %s", chain, cinema_code, existing)
        return {"status": "skipped_exists", "row": existing}

    payload = {
        "cinema_code": cinema_code,
        "name": _display_name(chain, name),
        "chain": chain,
        "latitude": latitude,
        "longitude": longitude,
        "programming_mode": "commercial_only",
        "is_arthouse_venue": False,
    }
    if dry_run:
        logger.info("DRY RUN: would insert %s", payload)
        return {"status": "dry_run", "payload": payload}

    supabase.client.table("cinemas").insert(payload).execute()
    logger.info("Inserted %s/%s", chain, cinema_code)
    return {"status": "inserted", "payload": payload}


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
    existing = _fetch_existing_cinema(supabase, chain, cinema_code)
    if existing:
        logger.info("Skip: %s/%s already in DB: %s", chain, cinema_code, existing)
        return {"status": "skipped_exists", "row": existing}

    logger.info("Looking up place %r ...", query)
    lat, lng, road_address = lookup_place_kr(query)
    logger.info("  -> lat=%s lng=%s (via %r)", lat, lng, road_address)

    return _insert_commercial_cinema(
        supabase, chain, cinema_code, name, lat, lng, dry_run=dry_run
    )


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


def sync_megabox(
    *,
    region: str | None = "10",
    limit: int | None = None,
    skip_premium: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Diff Megabox's live cinema list against the DB and insert new Seoul
    entries as commercial-only / non-arthouse venues.
    """
    del skip_premium
    from crawlers.megabox import fetch_megabox_cinema_list

    sites = fetch_megabox_cinema_list(area_code=region)
    skipped_closed = [s for s in sites if "영업종료" in s["brchNm"]]
    active_sites = [s for s in sites if s not in skipped_closed]
    supabase = SupabaseClient()
    existing_codes = {c["cinema_code"] for c in supabase.fetch_cinemas(chain="Megabox")}

    new_sites = [s for s in active_sites if s["brchNo"] not in existing_codes]
    to_process = new_sites[:limit] if limit else new_sites

    added: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for s in to_process:
        try:
            r = add_cinema_one_shot(
                "Megabox", s["brchNo"], s["brchNm"], dry_run=dry_run
            )
            added.append({"brchNo": s["brchNo"], "brchNm": s["brchNm"], **r})
        except (GeocodingError, Exception) as e:
            errors.append(
                {"brchNo": s["brchNo"], "brchNm": s["brchNm"], "error": str(e)}
            )

    return {
        "chain": "Megabox",
        "region": region,
        "discovered": len(sites),
        "active_discovered": len(active_sites),
        "skipped_closed": skipped_closed,
        "existing_in_db": len(active_sites) - len(new_sites),
        "new": len(new_sites),
        "processed": len(to_process),
        "dry_run": dry_run,
        "added": added,
        "errors": errors,
    }


def sync_lotte(
    *,
    region: str | None = "0001",
    limit: int | None = None,
    skip_premium: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Diff Lotte Cinema's live cinema list against the DB and insert new
    Seoul entries as commercial-only / non-arthouse venues.
    """
    del skip_premium
    from crawlers.lotte import fetch_lotte_cinema_list

    sites = fetch_lotte_cinema_list(detail_division_code=region)
    supabase = SupabaseClient()
    existing_codes = {c["cinema_code"] for c in supabase.fetch_cinemas(chain="Lotte")}

    def cinema_code(site: dict) -> str:
        return f"1|1|{site['CinemaID']}"

    new_sites = [s for s in sites if cinema_code(s) not in existing_codes]
    to_process = new_sites[:limit] if limit else new_sites

    added: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for s in to_process:
        code = cinema_code(s)
        name = str(s.get("CinemaNameKR") or "").strip()
        try:
            r = _insert_commercial_cinema(
                supabase,
                "Lotte",
                code,
                name,
                float(s["Latitude"]),
                float(s["Longitude"]),
                dry_run=dry_run,
            )
            added.append({"CinemaID": s["CinemaID"], "CinemaNameKR": name, **r})
        except Exception as e:
            errors.append({"CinemaID": s.get("CinemaID"), "CinemaNameKR": name, "error": str(e)})

    return {
        "chain": "Lotte",
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
    "Megabox": sync_megabox,
    "Lotte": sync_lotte,
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
        default="default",
        help="Region code to limit discovery. Defaults by chain: CGV 01, "
        'Megabox 10, Lotte 0001. Pass "all" to disable filtering.',
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
            kwargs: dict[str, Any] = {
                "limit": args.limit,
                "skip_premium": not args.include_premium,
                "dry_run": args.dry_run,
            }
            if args.region != "default":
                kwargs["region"] = None if args.region == "all" else args.region
            fn = _CHAIN_SYNC_FNS[args.chain]
            result = fn(**kwargs)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if not result.get("errors") else 1
    except GeocodingError as e:
        print(f"Geocoding failed: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(_main())
