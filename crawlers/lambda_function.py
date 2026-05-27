import asyncio
from crawlers.crawler_registry import CrawlerRegistry
from crawlers.supabase_client import SupabaseClient

_MAX_CHAIN_RETRIES = 1


def lambda_handler(event, context):
    chains = event.get(
        "chains",
        ["CGV", "Megabox", "Lotte", "CineQ", "TinyTicket", "Dtryx", "Moviee", "KOFA"],
    )
    supabase = SupabaseClient()

    async def run_one(chain: str):
        try:
            crawler = CrawlerRegistry.get_crawler(chain, supabase)
            print(f"▶ Running crawler for {chain}...")
            screenings = await crawler.run()
            print(f"✔ {chain}: Crawled {len(screenings)} screenings")
            await crawler.save_to_db(screenings)
            return chain, None
        except Exception as e:
            print(f"❌ Error with {chain}: {e}")
            return chain, e

    async def run_all():
        results = dict(await asyncio.gather(*[run_one(c) for c in chains]))

        for retry_round in range(1, _MAX_CHAIN_RETRIES + 1):
            failed = [c for c, err in results.items() if err is not None]
            if not failed:
                break
            print(f"⟳ Retry {retry_round}/{_MAX_CHAIN_RETRIES}: re-running {failed}")
            retried = dict(await asyncio.gather(*[run_one(c) for c in failed]))
            results.update(retried)

        return results

    results = asyncio.run(run_all())
    failed = [c for c, err in results.items() if err is not None]
    succeeded = [c for c, err in results.items() if err is None]

    if failed:
        raise RuntimeError(f"Failed chains: {failed}")
    return {
        "statusCode": 200,
        "body": f"OK: {succeeded}"
    }
