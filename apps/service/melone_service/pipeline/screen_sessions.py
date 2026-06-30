from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from melone_service.models import NormalizedEvent, utc_timestamp
from melone_service.pipeline.context_pages import ContextUnit, normalize_context_page
from melone_service.store.screen import ScreenRepository, ScreenSession
from melone_service.store.ocr_jobs import OcrJobRepository


@dataclass(frozen=True)
class ScreenContext:
    app_name: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None
    url: str | None = None
    timestamp: datetime | str | None = None
    evidence_event_ids: Sequence[str] = ()


class ScreenSessionizer:
    def __init__(
        self,
        *,
        screen_repository: ScreenRepository,
        job_repository: OcrJobRepository,
    ) -> None:
        self.screen_repository = screen_repository
        self.job_repository = job_repository

    def accept_latest_context(
        self,
        context: ContextUnit | ScreenContext | NormalizedEvent,
        *,
        now: datetime | str | None = None,
    ) -> ScreenSession:
        unit = _context_unit(context)
        page = normalize_context_page(unit)
        retrieval_locator = page.retrieval_locator or page.source_key

        open_session = self.screen_repository.get_latest_open_session()
        if open_session is not None and (
            open_session.retrieval_locator == retrieval_locator
        ):
            touched_session = self.screen_repository.touch_session(
                open_session.id,
                app_name=unit.app_name,
                bundle_id=unit.bundle_id,
                window_title=unit.window_title,
                url=unit.url,
                now=now or unit.started_at,
            )
            if touched_session is not None:
                return touched_session

        if open_session is not None:
            self._close_session(open_session.id, ended_at=unit.started_at)

        return self.screen_repository.create_session(
            source_key=page.source_key,
            retrieval_locator=retrieval_locator,
            app_name=unit.app_name,
            bundle_id=unit.bundle_id,
            window_title=unit.window_title,
            url=unit.url,
            started_at=unit.started_at,
            now=now or unit.started_at,
        )

    def close_current_session(
        self,
        *,
        ended_at: datetime | str | None = None,
    ) -> ScreenSession | None:
        open_session = self.screen_repository.get_latest_open_session()
        if open_session is None:
            return None
        return self._close_session(open_session.id, ended_at=ended_at)

    def _close_session(
        self,
        session_id: str,
        *,
        ended_at: datetime | str | None = None,
    ) -> ScreenSession | None:
        closed_session = self.screen_repository.close_session(
            session_id,
            ended_at=ended_at,
        )
        if closed_session is None:
            return None

        self.job_repository.create_pending_job(
            job_type="session_finalize",
            target_id=closed_session.id,
            session_id=closed_session.id,
            source_key=closed_session.source_key,
            retrieval_locator=closed_session.retrieval_locator,
            next_run_at=closed_session.ended_at,
            now=closed_session.ended_at,
            metadata={"closed_at": closed_session.ended_at},
        )
        return closed_session


def _context_unit(context: ContextUnit | ScreenContext | NormalizedEvent) -> ContextUnit:
    if isinstance(context, ContextUnit):
        return context

    if isinstance(context, NormalizedEvent):
        return ContextUnit(
            app_name=context.app_name,
            bundle_id=context.bundle_id,
            window_title=context.window_title,
            url=context.url,
            started_at=context.timestamp,
            ended_at=None,
            evidence_event_ids=[context.id],
        )

    timestamp = _timestamp(context.timestamp)
    return ContextUnit(
        app_name=context.app_name,
        bundle_id=context.bundle_id,
        window_title=context.window_title,
        url=context.url,
        started_at=timestamp,
        ended_at=None,
        evidence_event_ids=list(context.evidence_event_ids),
    )


def _timestamp(value: datetime | str | None) -> str:
    if value is None:
        return utc_timestamp()
    if isinstance(value, str):
        return value
    return utc_timestamp(value)
