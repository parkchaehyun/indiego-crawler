from supabase import create_client, Client
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from models import Screening


class SupabaseClient:
    def __init__(self):
        """Initialize Supabase client using environment variables."""
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_KEY")
        if not url or not key:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
        self.client: Client = create_client(url, key)

    def insert_screenings(self, data: list["Screening"], *, chunk_size: int = 500):
        """Insert screenings into Supabase, chunked to avoid statement timeouts.

        Each row triggers movie-reconciliation logic (per-row triggers from
        step3/step7/step18 migrations), so a single ~8k upsert exceeds the
        default 8s statement_timeout. Chunking to 500 keeps each batch well
        under the limit while still being efficient.
        """
        unique_map = {}
        for s in data:
            key = (s.provider, s.cinema_code, s.play_date, s.start_dt, s.screen_name)
            unique_map[key] = s  # Last one wins

        payload = [s.model_dump(exclude_none=True) for s in unique_map.values()]
        if not payload:
            return

        total = len(payload)
        for i in range(0, total, chunk_size):
            batch = payload[i:i + chunk_size]
            (
                self.client.table("screenings")
                .upsert(batch, on_conflict="provider,cinema_code,play_date,start_dt,screen_name")
                .execute()
            )
            print(f"  upserted {min(i + chunk_size, total)}/{total}")

    def fetch_cinemas(self, chain: str | None = None) -> list[dict[str, Any]]:
        """Fetch cinemas from Supabase, optionally filtered by chain."""
        query = self.client.table("cinemas").select("*")
        if chain:
            query = query.eq("chain", chain)
        response = query.execute()
        return response.data

    def insert_cinemas(self, cinemas: list[dict[str, Any]]) -> None:
        """Insert cinemas into Supabase."""
        self.client.table("cinemas").insert(cinemas).execute()
