from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from melone_service.models import utc_now, utc_timestamp
from melone_service.pipeline.activity import ACTIVITY_EVENT_TYPES
from melone_service.pipeline.context_graph import (
    ATTRIBUTION_MODEL_VERSION,
    ENGAGEMENT_MODEL_VERSION,
    rank_contexts,
)
from melone_service.pipeline.context_pages import (
    CONTEXT_GRAPH_EVENT_TYPES,
    RankedContextPage,
)
from melone_service.store.context_rank import ContextRankRepository, ContextRankScore
from melone_service.store.events import DEFAULT_EVENT_LIMIT, EventRepository


CONTEXT_RANK_CACHE_EVENT_TYPES = (*CONTEXT_GRAPH_EVENT_TYPES, *ACTIVITY_EVENT_TYPES)
DEFAULT_CONTEXT_RANK_CACHE_EVENT_LIMIT = DEFAULT_EVENT_LIMIT
CONTEXT_RANK_CACHE_MODEL_VERSION = (
    f"context_rank_cache_v1:"
    f"engagement={ENGAGEMENT_MODEL_VERSION}:"
    f"attribution={ATTRIBUTION_MODEL_VERSION}"
)


@dataclass(frozen=True)
class ContextRankCacheRefreshResult:
    upserted_count: int
    event_count: int
    computed_at: str
    model_version: str
    scores: tuple[ContextRankScore, ...]
    # False when refresh_if_due short-circuited (cadence not elapsed or no new
    # relevant events). The direct refresh() path always recomputes.
    recomputed: bool = True


class ContextRankCacheRefresher:
    def __init__(
        self,
        event_repository: EventRepository,
        context_rank_repository: ContextRankRepository,
        *,
        model_version: str = CONTEXT_RANK_CACHE_MODEL_VERSION,
    ) -> None:
        self.event_repository = event_repository
        self.context_rank_repository = context_rank_repository
        self.model_version = model_version

    def refresh(
        self,
        *,
        since: str | None = None,
        event_limit: int = DEFAULT_CONTEXT_RANK_CACHE_EVENT_LIMIT,
        now: datetime | None = None,
    ) -> ContextRankCacheRefreshResult:
        if event_limit <= 0:
            raise ValueError("context rank cache event_limit must be greater than zero")

        events = self.event_repository.list_recent_by_types(
            CONTEXT_RANK_CACHE_EVENT_TYPES,
            since=since,
            limit=event_limit,
        )
        ranked_contexts = rank_contexts(events, show_hidden=False)
        computed_at = utc_timestamp(now)
        scores = tuple(
            _score_from_ranked_context(
                ranked_context,
                computed_at=computed_at,
                model_version=self.model_version,
            )
            for ranked_context in ranked_contexts
        )
        upserted_count = self.context_rank_repository.upsert_scores(scores)

        return ContextRankCacheRefreshResult(
            upserted_count=upserted_count,
            event_count=len(events),
            computed_at=computed_at,
            model_version=self.model_version,
            scores=scores,
            recomputed=True,
        )

    def refresh_if_due(
        self,
        *,
        min_interval_seconds: float,
        now: datetime | None = None,
        event_limit: int = DEFAULT_CONTEXT_RANK_CACHE_EVENT_LIMIT,
    ) -> ContextRankCacheRefreshResult:
        """Recompute only when worthwhile, returning recomputed=False otherwise.

        ``rank_contexts`` is a pure function of the event window, so when no new
        relevant event has arrived since the last computed_at the scores are
        provably identical and recomputing is wasted work. A cadence floor
        (``min_interval_seconds``) further bounds cost during active use, where
        new events arrive on nearly every tick. The first run (no prior
        computed_at) always recomputes.
        """
        now = utc_now() if now is None else now
        last_computed_at = self.context_rank_repository.latest_computed_at()
        if last_computed_at is not None:
            elapsed = (now - _parse_timestamp(last_computed_at)).total_seconds()
            if elapsed < min_interval_seconds:
                return self._skipped_result(last_computed_at)

            latest_event = self.event_repository.latest_by_types(
                CONTEXT_RANK_CACHE_EVENT_TYPES
            )
            # Parse both sides (cheap — once per tick) so the comparison is robust
            # to any timestamp-format difference rather than assuming identical ISO
            # precision. An event in the same instant as last_computed_at compares
            # equal and is treated as already-incorporated; that is self-healing —
            # the next relevant event (or the min_interval ceiling) recomputes and
            # includes it, well within the freshness budget.
            if latest_event is None or (
                _parse_timestamp(latest_event.timestamp)
                <= _parse_timestamp(last_computed_at)
            ):
                return self._skipped_result(last_computed_at)

        return self.refresh(event_limit=event_limit, now=now)

    def _skipped_result(self, computed_at: str) -> ContextRankCacheRefreshResult:
        return ContextRankCacheRefreshResult(
            upserted_count=0,
            event_count=0,
            computed_at=computed_at,
            model_version=self.model_version,
            scores=(),
            recomputed=False,
        )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _score_from_ranked_context(
    ranked_context: RankedContextPage,
    *,
    computed_at: str,
    model_version: str,
) -> ContextRankScore:
    return ContextRankScore(
        source_key=ranked_context.page.source_key,
        score=ranked_context.score,
        visits=ranked_context.visits,
        retrieval_locators=ranked_context.retrieval_locators,
        computed_at=computed_at,
        model_version=model_version,
    )
