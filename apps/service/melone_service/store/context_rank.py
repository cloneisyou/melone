from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ContextRankScore:
    source_key: str
    score: float
    visits: int
    retrieval_locators: tuple[str, ...]
    computed_at: str
    model_version: str
    created_at: str | None = None
    updated_at: str | None = None


class ContextRankRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def count_scores(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM context_rank_scores"
        ).fetchone()
        return int(row[0])

    def latest_computed_at(self) -> str | None:
        row = self.connection.execute(
            """
            SELECT computed_at
            FROM context_rank_scores
            ORDER BY computed_at DESC, source_key ASC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else str(row["computed_at"])

    def upsert_scores(self, scores: Sequence[ContextRankScore]) -> int:
        if not scores:
            return 0

        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO context_rank_scores (
                  source_key,
                  score,
                  visits,
                  retrieval_locators_json,
                  computed_at,
                  model_version
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                  score = excluded.score,
                  visits = excluded.visits,
                  retrieval_locators_json = excluded.retrieval_locators_json,
                  computed_at = excluded.computed_at,
                  model_version = excluded.model_version,
                  updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                [
                    (
                        score.source_key,
                        score.score,
                        score.visits,
                        stable_retrieval_locators_json(
                            score.retrieval_locators
                        ),
                        score.computed_at,
                        score.model_version,
                    )
                    for score in scores
                ],
            )
        return len(scores)

    def get_score(self, source_key: str) -> ContextRankScore | None:
        row = self.connection.execute(
            """
            SELECT
              source_key,
              score,
              visits,
              retrieval_locators_json,
              computed_at,
              model_version,
              created_at,
              updated_at
            FROM context_rank_scores
            WHERE source_key = ?
            """,
            (source_key,),
        ).fetchone()
        return None if row is None else _row_to_score(row)

    def list_scores_for_source_keys(
        self,
        source_keys: Sequence[str],
    ) -> dict[str, ContextRankScore]:
        unique_source_keys = tuple(
            dict.fromkeys(
                source_key
                for source_key in source_keys
                if str(source_key).strip()
            )
        )
        if not unique_source_keys:
            return {}

        placeholders = ",".join("?" for _ in unique_source_keys)
        rows = self.connection.execute(
            f"""
            SELECT
              source_key,
              score,
              visits,
              retrieval_locators_json,
              computed_at,
              model_version,
              created_at,
              updated_at
            FROM context_rank_scores
            WHERE source_key IN ({placeholders})
            """,
            unique_source_keys,
        ).fetchall()
        return {row["source_key"]: _row_to_score(row) for row in rows}

    def list_scores(self, *, limit: int | None = None) -> list[ContextRankScore]:
        query = """
            SELECT
              source_key,
              score,
              visits,
              retrieval_locators_json,
              computed_at,
              model_version,
              created_at,
              updated_at
            FROM context_rank_scores
            ORDER BY score DESC, visits DESC, source_key ASC
        """
        params: list[object] = []
        if limit is not None:
            if limit <= 0:
                raise ValueError("context rank score limit must be greater than zero")
            query += " LIMIT ?"
            params.append(limit)

        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_score(row) for row in rows]


def stable_retrieval_locators_json(retrieval_locators: Sequence[str]) -> str:
    locators = sorted(
        {
            str(locator).strip()
            for locator in retrieval_locators
            if str(locator).strip()
        }
    )
    return json.dumps(locators, ensure_ascii=False, separators=(",", ":"))


def _row_to_score(row: sqlite3.Row) -> ContextRankScore:
    return ContextRankScore(
        source_key=row["source_key"],
        score=float(row["score"]),
        visits=int(row["visits"]),
        retrieval_locators=_retrieval_locators_from_json(
            row["retrieval_locators_json"]
        ),
        computed_at=row["computed_at"],
        model_version=row["model_version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _retrieval_locators_from_json(value: str) -> tuple[str, ...]:
    locators = json.loads(value)
    if not isinstance(locators, list) or not all(
        isinstance(locator, str) for locator in locators
    ):
        raise ValueError("retrieval_locators_json must contain a JSON string array")
    return tuple(locators)
