from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


DEFAULT_SCREEN_SEARCH_LIMIT = 20
MAX_SCREEN_SEARCH_LIMIT = 100
MAX_FTS_QUERY_TERMS = 12
MIN_FTS_TRIGRAM_TERM_LENGTH = 3

_QUERY_TERM_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class ScreenSearchCandidate:
    chunk_id: str
    session_id: str
    frame_id: str
    source_key: str
    retrieval_locator: str
    app_name: str | None
    window_title: str | None
    url: str | None
    session_started_at: str
    session_ended_at: str | None
    chunk_created_at: str
    text: str
    raw_bm25: float
    bm25_relevance: float


class ScreenSearchRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def search_chunks(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SCREEN_SEARCH_LIMIT,
        since: str | None = None,
    ) -> list[ScreenSearchCandidate]:
        fts_query = build_fts_query(query)
        if fts_query is None:
            return []

        clauses = ["ocr_chunks_fts MATCH ?"]
        params: list[object] = [fts_query]
        if since is not None:
            clauses.append("COALESCE(s.ended_at, s.started_at) >= ?")
            params.append(since)
        params.append(_coerce_limit(limit))

        rows = self.connection.execute(
            f"""
            SELECT
              c.id AS chunk_id,
              c.session_id,
              c.frame_id,
              c.source_key,
              c.retrieval_locator,
              c.app_name,
              c.window_title,
              c.url,
              s.started_at AS session_started_at,
              s.ended_at AS session_ended_at,
              c.created_at AS chunk_created_at,
              c.text,
              bm25(ocr_chunks_fts) AS raw_bm25
            FROM ocr_chunks_fts
            JOIN ocr_chunks AS c
              ON c.id = ocr_chunks_fts.chunk_id
            JOIN screen_sessions AS s
              ON s.id = c.session_id
            WHERE {" AND ".join(clauses)}
            ORDER BY raw_bm25 ASC, c.created_at DESC, c.id ASC
            LIMIT ?
            """,
            params,
        ).fetchall()

        return _candidates_from_rows(rows)


def build_fts_query(query: str) -> str | None:
    normalized = normalize_search_query(query)
    if not normalized:
        return None

    terms = extract_query_terms(normalized)
    if terms:
        return " ".join(_quote_fts_phrase(term) for term in terms)

    if len(normalized) < MIN_FTS_TRIGRAM_TERM_LENGTH:
        return None
    return _quote_fts_phrase(normalized)


def normalize_search_query(query: str) -> str:
    return " ".join(str(query).split())


def extract_query_terms(query: str) -> list[str]:
    normalized = normalize_search_query(query)
    terms: list[str] = []
    seen: set[str] = set()
    for match in _QUERY_TERM_RE.finditer(normalized):
        term = match.group(0)
        if len(term) < MIN_FTS_TRIGRAM_TERM_LENGTH:
            continue

        dedupe_key = term.casefold()
        if dedupe_key in seen:
            continue

        terms.append(term)
        seen.add(dedupe_key)
        if len(terms) >= MAX_FTS_QUERY_TERMS:
            break

    return terms


def _quote_fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _coerce_limit(limit: int) -> int:
    if limit <= 0:
        raise ValueError("search limit must be greater than zero")
    return min(limit, MAX_SCREEN_SEARCH_LIMIT)


def _candidates_from_rows(rows: list[sqlite3.Row]) -> list[ScreenSearchCandidate]:
    if not rows:
        return []

    raw_scores = [float(row["raw_bm25"]) for row in rows]
    best_score = min(raw_scores)
    worst_score = max(raw_scores)
    score_range = worst_score - best_score

    candidates: list[ScreenSearchCandidate] = []
    for row in rows:
        raw_bm25 = float(row["raw_bm25"])
        if score_range == 0:
            bm25_relevance = 1.0
        else:
            bm25_relevance = (worst_score - raw_bm25) / score_range

        candidates.append(
            ScreenSearchCandidate(
                chunk_id=row["chunk_id"],
                session_id=row["session_id"],
                frame_id=row["frame_id"],
                source_key=row["source_key"],
                retrieval_locator=row["retrieval_locator"],
                app_name=row["app_name"],
                window_title=row["window_title"],
                url=row["url"],
                session_started_at=row["session_started_at"],
                session_ended_at=row["session_ended_at"],
                chunk_created_at=row["chunk_created_at"],
                text=row["text"],
                raw_bm25=raw_bm25,
                bm25_relevance=max(0.0, min(1.0, bm25_relevance)),
            )
        )

    return candidates
