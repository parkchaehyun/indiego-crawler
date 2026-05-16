import argparse
import asyncio
import json
from pathlib import Path

from crawlers.crawler_registry import CrawlerRegistry


AVAILABLE_CHAINS = ("CGV", "Megabox", "Lotte", "Dtryx", "Moviee", "TinyTicket", "KOFA")


class DummySupabase:
    def __init__(self, root_dir: Path):
        self.root_dir = root_dir

    def fetch_cinemas(self, chain=None):
        cinemas_path = self.root_dir / "cinemas.json"
        with cinemas_path.open(encoding="utf-8") as file:
            cinemas = json.load(file)
        if chain is None:
            return cinemas
        return [cinema for cinema in cinemas if cinema.get("chain") == chain]

    def insert_screenings(self, data):
        print(f"[DummySupabase] insert {len(data)} rows")


async def run_chain(
    chain: str,
    batch_size: int,
    output_path: Path,
):
    crawler = CrawlerRegistry.get_crawler(
        chain=chain, supabase=DummySupabase(Path.cwd()), batch_size=batch_size
    )
    screenings = await crawler.run()
    payload = [screening.model_dump() for screening in screenings]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Chain: {chain}")
    print(f"Rows: {len(payload)}")
    print(f"Output: {output_path}")
    print("Sample titles:")
    for row in payload[:10]:
        print(f" - {row.get('movie_title')}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run a local crawler with cinemas.json + DummySupabase."
    )
    parser.add_argument(
        "--chain",
        choices=AVAILABLE_CHAINS,
        required=True,
        help="Crawler chain name.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Crawler batch size. Default: 10.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON file path. Default: <chain>_screenings_local.json",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output = (
        Path(args.output)
        if args.output
        else Path(f"{args.chain.lower()}_screenings_local.json")
    )

    asyncio.run(
        run_chain(
            chain=args.chain,
            batch_size=args.batch_size,
            output_path=output,
        )
    )


if __name__ == "__main__":
    main()
