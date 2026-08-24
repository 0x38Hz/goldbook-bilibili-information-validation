"""Cached-price outcome recomputation without media or model work."""

from dataclasses import dataclass

from goldbook.db import Database
from goldbook.models import PriceBar
from goldbook.scoring import score_signal


@dataclass(frozen=True)
class OutcomeRecomputationSummary:
    upserted: int = 0
    deleted: int = 0


def recompute_cached_outcomes(database: Database) -> OutcomeRecomputationSummary:
    """Re-score every stored video from SQLite analyses and prices only."""
    bars = tuple(PriceBar(*row) for row in database.list_prices())
    upserted = 0
    deleted = 0
    for video, analysis in database.list_videos_with_latest_analysis():
        if analysis is None:
            deleted += int(database.delete_outcome(video.bvid))
            continue
        outcome = score_signal(analysis, video.published_at, bars)
        if outcome.entry_date is None or outcome.entry_price is None:
            deleted += int(database.delete_outcome(video.bvid))
            continue
        database.save_outcome(outcome)
        upserted += 1
    return OutcomeRecomputationSummary(upserted=upserted, deleted=deleted)
