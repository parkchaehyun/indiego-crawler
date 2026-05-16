import abc
import logging
from typing import List, get_args

from models import Chain, Cinema, Screening

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BaseCrawler(abc.ABC):
    chain: Chain

    def __init__(self, supabase=None, batch_size: int = 10):
        if not hasattr(self, "chain") or self.chain not in get_args(Chain):
            raise ValueError(f"Invalid chain: {getattr(self, 'chain', None)}")

        self.supabase = supabase
        self.batch_size = batch_size
        self.theaters: List[Cinema] = self.load_theaters()

    def load_theaters(self) -> list[Cinema]:
        """Load theaters for `self.chain` from Supabase (the authoritative source).

        Live DB read means new cinemas added via `crawlers.sync_cinemas` are
        picked up on the next crawl invocation with no redeploy needed.
        """
        if self.supabase is None:
            raise RuntimeError("BaseCrawler requires a SupabaseClient to load theaters")
        raw = self.supabase.fetch_cinemas(chain=self.chain)
        data = [Cinema(**c) for c in raw]
        logger.info("Loaded %d %s theaters from Supabase", len(data), self.chain)
        return data

    async def save_to_db(self, screenings: List) -> None:
        if not screenings:
            return
        try:
            self.supabase.insert_screenings(screenings)
            print(f"✅ Supabase insert successful for {self.chain}")
        except Exception as exc:
            print(f"❌ Supabase save error for {self.chain}: {exc}")
            raise

    @abc.abstractmethod
    async def run(self) -> list[Screening]:
        """Return every bookable screening this chain currently exposes.

        Each crawler discovers the operational date list per theater from the
        chain's own API rather than iterating a fixed window.
        """
        ...
