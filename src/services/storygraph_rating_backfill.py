"""One-time backfill for StoryGraph rating fields on already-linked books.

Runs in a background daemon thread at startup. Iterates every StoryGraph
linked book whose rating has never been fetched (``storygraph_rating_updated_at
IS NULL``), fetches it, and saves it back. Self-limiting: once a row has a
non-null ``storygraph_rating_updated_at`` it is skipped forever, so future
startups are no-ops.
"""

import logging
import threading
import time
from typing import Optional

from src.api.storygraph_client import StorygraphClient
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

_DEFAULT_REQUEST_DELAY_SEC = 1.5


class StorygraphRatingBackfill:
    def __init__(
        self,
        database_service: DatabaseService,
        storygraph_client: StorygraphClient,
        request_delay_sec: float = _DEFAULT_REQUEST_DELAY_SEC,
    ):
        self.database_service = database_service
        self.storygraph_client = storygraph_client
        self.request_delay_sec = request_delay_sec

    def _candidates(self) -> list:
        all_details = self.database_service.get_all_storygraph_details() or []
        return [
            d for d in all_details
            if getattr(d, "storygraph_book_id", None)
            and getattr(d, "storygraph_rating_updated_at", None) is None
        ]

    def run(self) -> None:
        if not self.storygraph_client.is_configured():
            return

        try:
            candidates = self._candidates()
        except Exception as exc:
            logger.warning("StoryGraph rating backfill: failed to query candidates: %s", exc, exc_info=True)
            return

        if not candidates:
            return

        logger.info(
            "StoryGraph rating backfill: starting one-time fetch for %d linked book(s)",
            len(candidates),
        )

        updated = 0
        failed = 0
        for details in candidates:
            book_id = str(details.storygraph_book_id)
            try:
                rating_info = self.storygraph_client.get_book_rating(book_id) or {}
            except Exception as exc:
                logger.warning(
                    "StoryGraph rating backfill: fetch failed for %s (%s): %s",
                    details.abs_id, book_id, exc, exc_info=True,
                )
                failed += 1
                time.sleep(self.request_delay_sec)
                continue

            rating = rating_info.get("rating") if isinstance(rating_info, dict) else None
            review_count = rating_info.get("review_count") if isinstance(rating_info, dict) else None

            if rating is None and review_count is None:
                time.sleep(self.request_delay_sec)
                continue

            details.storygraph_rating = rating
            details.storygraph_review_count = review_count
            details.storygraph_rating_updated_at = time.time()

            try:
                self.database_service.save_storygraph_details(details)
                updated += 1
            except Exception as exc:
                logger.warning(
                    "StoryGraph rating backfill: save failed for %s: %s",
                    details.abs_id, exc, exc_info=True,
                )
                failed += 1

            time.sleep(self.request_delay_sec)

        logger.info(
            "StoryGraph rating backfill: complete — updated=%d, failed=%d, skipped=%d",
            updated, failed, len(candidates) - updated - failed,
        )


def start_backfill_thread(
    database_service: DatabaseService,
    storygraph_client: StorygraphClient,
    request_delay_sec: float = _DEFAULT_REQUEST_DELAY_SEC,
    initial_delay_sec: float = 30.0,
) -> Optional[threading.Thread]:
    """Launch the backfill in a daemon thread. Returns the thread, or None if SG is not configured."""
    if not storygraph_client.is_configured():
        return None

    backfill = StorygraphRatingBackfill(
        database_service=database_service,
        storygraph_client=storygraph_client,
        request_delay_sec=request_delay_sec,
    )

    def _runner():
        if initial_delay_sec > 0:
            time.sleep(initial_delay_sec)
        try:
            backfill.run()
        except Exception as exc:
            logger.warning("StoryGraph rating backfill: unexpected error: %s", exc, exc_info=True)

    thread = threading.Thread(target=_runner, daemon=True, name="StorygraphRatingBackfill")
    thread.start()
    return thread
