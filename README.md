# Indiego Crawler & TMDB Poster Updater

## Overview

This repo contains two AWS Lambda container functions:

1. `crawler` Lambda
- Crawls screenings from: `CGV`, `Megabox`, `Lotte`, `Dtryx`, `Moviee`, `TinyTicket`, `KOFA`
- Normalizes rows into a shared `Screening` model
- Upserts into Supabase (`screenings`, with `cinemas` as reference data)

2. `tmdb` Lambda
- Finds upcoming movies that still have no `tmdb_id`
- Searches TMDB using canonical EN/KO title seeds
- Writes `tmdb_id` + `poster_url` back to `movies`
- Optionally reconciles duplicate movie rows via DB RPCs

---

## Related Links

- Web UI repo: https://github.com/parkchaehyun/cinema-site
- Production site: https://indiego.ing

---

## Current Architecture

```text
EventBridge (schedule)
  -> Lambda container (crawler)
  -> Supabase (screenings/cinemas/movies/...)

EventBridge (schedule)
  -> Lambda container (tmdb updater)
  -> Supabase movies (tmdb_id, poster_url)
```

Key runtime facts:
- Crawler image uses Playwright Python base image with Chromium.
- TMDB image is lightweight (`public.ecr.aws/lambda/python:3.11`).
- Default crawler event:
  - `chains`: `["CGV", "Megabox", "Lotte", "TinyTicket", "Dtryx", "Moviee", "KOFA"]`
- Each crawler discovers its own operational date list from the chain's API,
  so there is no fixed-window parameter — every bookable screening is fetched.

---

## Prerequisites

- Docker 20.10+
- AWS CLI v2 (for ECR/Lambda deployment)
- Supabase project with the required schema/views/functions
- TMDB Bearer token (`TMDB_API_KEY`)

---

## Data Model (Code)

`models.py` currently defines:
- `Chain = Literal["CGV", "Megabox", "Lotte", "TinyTicket", "Dtryx", "Moviee", "KOFA"]`
- `Screening` fields include:
  - core fields: `provider`, `cinema_name`, `cinema_code`, `screen_name`, `movie_title`, `play_date`, `start_dt`, `end_dt`
  - enrichment: `movie_title_en`, `source_movie_code`, `source_year`, `source_director`
  - curation: `is_core_art_screen`
  - metadata: `crawl_ts`, `url`, `remain_seat_cnt`, `total_seat_cnt`

---

## Environment Variables

Required:
- `SUPABASE_URL`
- `SUPABASE_KEY`

Crawler optional:
- `KOFA_SERVICE_KEY` (required for KOFA data)
- `CGV_SIGN_SECRET` (required for CGV; HMAC secret extracted from CGV's JS bundle)
- `WEBSHARE_API_KEY` (optional proxy pool for CGV)
- `CGV_PROXY_COUNT` (number of parallel proxies, default `4`)

TMDB updater required:
- `TMDB_API_KEY` (TMDB v4 Bearer token)

---

## Database Expectations (Supabase)

This codebase assumes your DB already has the schema/views/functions used by the app and updater (migrations are in `migrations/`).

At minimum, current code expects:
- `screenings` table with fields used by crawler payload (including `movie_title_en`, `source_movie_code`, `source_year`, `source_director`, `is_core_art_screen`)
- `cinemas` reference table
- `movies` table with `id`, `title`, `canonical_title`, and `tmdb_id`/`poster_url` fields used by updater
- `upcoming_movie_ids` view (used by poster updater)

Optional but used when present:
- RPC `reconcile_movies_with_tmdb_anchor()`
- RPC `merge_movie_rows(keep_movie_id, drop_movie_id)`

If your DB is older, apply the SQL in `migrations/` before deploying these images.

---

## Repository Structure

```text
root/
├── crawlers/
│   ├── base.py
│   ├── cgv.py
│   ├── crawler_registry.py
│   ├── dtryx.py
│   ├── kofa.py
│   ├── lambda_function.py
│   ├── lotte.py
│   ├── megabox.py
│   ├── moviee.py
│   ├── offline_test.py
│   ├── poster_updater.py
│   ├── supabase_client.py
│   └── tinyticket.py
├── migrations/
├── cinemas.json
├── models.py
├── requirements-crawler.txt
├── requirements-tmdb.txt
├── Dockerfile
└── README.md
```

Note:
- `moonhwain.py` may exist as legacy/local code, but it is not registered in current `CrawlerRegistry`.

---

## Build Images

### Crawler image

```bash
docker buildx build \
  --platform linux/amd64 \
  --target crawler \
  --tag lambda-crawler:latest \
  --load \
  .
```

### TMDB updater image

```bash
docker buildx build \
  --platform linux/amd64 \
  --target tmdb \
  --tag lambda-tmdb:latest \
  --load \
  .
```

---

## Deploy (ECR/Lambda)

1. Push `lambda-crawler:latest` and `lambda-tmdb:latest` to ECR
2. Point each Lambda function to the new image URI
3. Set environment variables per function
4. Trigger a manual invoke to verify logs before enabling schedules

---

## Local Runs

### Run a single chain quickly

```bash
PYTHONPATH=. python -m crawlers.offline_test --chain Megabox
```

Writes the result to `megabox_screenings_local.json`. Pass `--output PATH` to override.

### Invoke Lambda handler locally

```bash
python - <<'PY'
from crawlers.lambda_function import lambda_handler
print(lambda_handler({"chains":["CGV"]}, None))
PY
```

---

## Notes

- CGV anti-bot behavior can vary by environment/IP. The Webshare proxy pool is a CGV-specific knob (`WEBSHARE_API_KEY`, `CGV_PROXY_COUNT`).
- Poster updater now focuses on upcoming movies via `upcoming_movie_ids`, not all historical movies.

---

## License

[PolyForm Noncommercial License 1.0.0](./LICENSE)

Commercial use is not permitted. For commercial licensing, contact the repository owner.

Copyright (c) 2026 Chaehyun Park
