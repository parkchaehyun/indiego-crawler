import asyncio
from crawlers.crawler_registry import CrawlerRegistry
from crawlers.supabase_client import SupabaseClient

def lambda_handler(event, context):
    chains = event.get(
        "chains",
        ["CGV", "Megabox", "Lotte", "CineQ", "TinyTicket", "Dtryx", "Moviee", "KOFA"],
    )
    supabase = SupabaseClient()

    failed = []
    succeeded = []

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
        # Chains hit independent upstreams, so overlapping their crawl phases
        # cuts wall-clock without changing per-chain behavior. Supabase writes
        # naturally serialize on the shared client; that's fine since the crawl
        # dominates each chain's runtime.
        return await asyncio.gather(*[run_one(c) for c in chains])

    for chain, err in asyncio.run(run_all()):
        (failed if err is not None else succeeded).append(chain)

    if failed:
        # Raise so EventBridge/scheduled Lambda marks the invocation as failed.
        raise RuntimeError(f"Failed chains: {failed}")
    return {
        "statusCode": 200,
        "body": f"OK: {succeeded}"
    }
