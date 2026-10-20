"""M2-2 shared collection pool: each subscribed source is fetched once per cycle, entries land in one
deduplicated pool, and full text/metadata extraction (trafilatura) is cached and shared by every user."""
from .collector import cycle_key, item_text, run_cycle, run_cycle_sync, subscribed_sources, user_items  # noqa: F401
