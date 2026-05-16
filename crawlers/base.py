import abc
import json
import logging
from pathlib import Path
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
        """
        Load theaters from local JSON or fallback to Supabase.
        Filters by `self.chain`.
        """
        try:
            root_dir = Path(__file__).parent.parent
            json_path = root_dir / "cinemas.json"
            if json_path.exists():
                with open(json_path, encoding="utf-8") as fp:
                    data = [Cinema(**c) for c in json.load(fp) if c["chain"] == self.chain]
                logger.info("Loaded %d %s theaters from %s", len(data), self.chain, json_path)
                return data
            elif self.supabase:
                raw = self.supabase.fetch_cinemas(chain=self.chain)
                data = [Cinema(**c) for c in raw]
                logger.info("Loaded %d %s theaters from Supabase", len(data), self.chain)
                return data
        except Exception as exc:
            logger.error("Error loading theaters: %s", exc)
        return []

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
