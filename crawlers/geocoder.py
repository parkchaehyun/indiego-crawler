"""Korean place / address resolution.

Two helpers, two backends, two credential pairs (both Naver):

* `geocode_kr(address)`   -- strict Korean-address → (lat, lng)
                             via NCP `/map-geocode/v2/geocode`.
                             Use when you already have a 도로명/지번 address.

* `lookup_place_kr(query)` -- free-text place name (e.g. "CGV 영등포타임스퀘어")
                              → (lat, lng, road_address). Uses Naver Local
                              Search to find the place's address, then defers
                              to `geocode_kr` for the precise coordinates.
                              Use when you only know the venue name.

Env vars:
  NCP_API_KEY_ID, NCP_API_KEY              -- NCP API Gateway (geocoding)
  NAVER_SEARCH_CLIENT_ID,
  NAVER_SEARCH_CLIENT_SECRET               -- developers.naver.com Search API
"""

from __future__ import annotations

import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_GEOCODE_URL = "https://maps.apigw.ntruss.com/map-geocode/v2/geocode"
_LOCAL_SEARCH_URL = "https://openapi.naver.com/v1/search/local.json"


class GeocodingError(RuntimeError):
    """Raised when an address cannot be resolved to coordinates."""


def geocode_kr(address: str, *, timeout: float = 10.0) -> tuple[float, float]:
    """Resolve a Korean address to (latitude, longitude).

    Picks the first result from NCP's response. Raises GeocodingError if the
    service returns zero matches or fails — callers should treat that as a
    hard stop (don't insert a cinema row without verified coordinates).
    """
    key_id = os.environ.get("NCP_API_KEY_ID")
    key = os.environ.get("NCP_API_KEY")
    if not key_id or not key:
        raise GeocodingError("NCP_API_KEY_ID and NCP_API_KEY must be set")

    headers = {
        "x-ncp-apigw-api-key-id": key_id,
        "x-ncp-apigw-api-key": key,
        "Accept": "application/json",
    }
    params = {"query": address}

    resp = httpx.get(_GEOCODE_URL, headers=headers, params=params, timeout=timeout)
    if resp.status_code != 200:
        raise GeocodingError(
            f"NCP geocode HTTP {resp.status_code} for {address!r}: {resp.text[:200]}"
        )

    body = resp.json()
    if body.get("status") != "OK":
        raise GeocodingError(
            f"NCP geocode status={body.get('status')} for {address!r}: "
            f"{body.get('errorMessage') or body}"
        )

    addresses = body.get("addresses") or []
    if not addresses:
        raise GeocodingError(f"No geocode match for address: {address!r}")

    top = addresses[0]
    # NCP returns x=longitude, y=latitude, both as strings.
    try:
        lng = float(top["x"])
        lat = float(top["y"])
    except (KeyError, TypeError, ValueError) as e:
        raise GeocodingError(f"Malformed NCP geocode response for {address!r}: {e}")

    if len(addresses) > 1:
        logger.info(
            "NCP geocode returned %d candidates for %r; using first (%s)",
            len(addresses), address, top.get("roadAddress") or top.get("jibunAddress"),
        )

    return lat, lng


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _HTML_TAG_RE.sub("", s or "")


def lookup_place_kr(query: str, *, timeout: float = 10.0) -> tuple[float, float, str]:
    """Resolve a Korean place / venue name to (lat, lng, road_address).

    Calls Naver Local Search to pick the top match, then re-runs that match's
    roadAddress through NCP geocoding for precise WGS84 coordinates (Local
    Search's own mapx/mapy use a non-WGS84 projection that's brittle to convert).

    Raises GeocodingError if Local Search returns no match, or if the chosen
    match's address fails NCP geocoding.
    """
    client_id = os.environ.get("NAVER_SEARCH_CLIENT_ID")
    client_secret = os.environ.get("NAVER_SEARCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise GeocodingError(
            "NAVER_SEARCH_CLIENT_ID and NAVER_SEARCH_CLIENT_SECRET must be set"
        )

    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
        "Accept": "application/json",
    }
    params = {"query": query, "display": 5, "start": 1, "sort": "random"}

    resp = httpx.get(_LOCAL_SEARCH_URL, headers=headers, params=params, timeout=timeout)
    if resp.status_code != 200:
        raise GeocodingError(
            f"Naver Local Search HTTP {resp.status_code} for {query!r}: {resp.text[:200]}"
        )
    items = resp.json().get("items") or []
    if not items:
        raise GeocodingError(f"No place match for query: {query!r}")

    top = items[0]
    road_address = (top.get("roadAddress") or top.get("address") or "").strip()
    if not road_address:
        raise GeocodingError(
            f"Local Search match for {query!r} lacks an address: {top}"
        )

    title = _strip_html(top.get("title") or "")
    if len(items) > 1:
        logger.info(
            "Local Search returned %d candidates for %r; using %r (%s)",
            len(items), query, title, road_address,
        )

    lat, lng = geocode_kr(road_address, timeout=timeout)
    return lat, lng, road_address


if __name__ == "__main__":
    # Smoke test: python -m crawlers.geocoder address "<address>"
    #             python -m crawlers.geocoder place "<place name>"
    import sys
    if len(sys.argv) < 3 or sys.argv[1] not in {"address", "place"}:
        print("usage: python -m crawlers.geocoder {address|place} <query>")
        sys.exit(2)
    mode, query = sys.argv[1], " ".join(sys.argv[2:])
    if mode == "address":
        lat, lng = geocode_kr(query)
        print(f"{query} -> lat={lat}, lng={lng}")
    else:
        lat, lng, addr = lookup_place_kr(query)
        print(f"{query} -> lat={lat}, lng={lng} (via {addr!r})")
