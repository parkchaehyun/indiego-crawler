import asyncio
from crawlers.crawler_registry import CrawlerRegistry
from crawlers.supabase_client import SupabaseClient

def lambda_handler(event, context):
    chains = event.get(
        "chains",
        ["CGV", "Megabox", "Lotte", "TinyTicket", "Dtryx", "Moviee", "KOFA"],
    )
    supabase = SupabaseClient()

    failed = []
    succeeded = []

    async def run_all():
        for chain in chains:
            try:
                crawler = CrawlerRegistry.get_crawler(chain, supabase)
                print(f"▶ Running crawler for {chain}...")
                screenings = await crawler.run()
                print(f"✔ {chain}: Crawled {len(screenings)} screenings")
                await crawler.save_to_db(screenings)
                succeeded.append(chain)
            except Exception as e:
                print(f"❌ Error with {chain}: {e}")
                failed.append(chain)

    asyncio.run(run_all())

    if failed:
        # Raise so EventBridge/scheduled Lambda marks the invocation as failed.
        raise RuntimeError(f"Failed chains: {failed}")
    return {
        "statusCode": 200,
        "body": f"OK: {succeeded}"
    }
