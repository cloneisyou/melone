from __future__ import annotations

from dataclasses import dataclass

from melone_service.embeddings.model import EmbeddingModel, EmbeddingVector
from melone_service.store.embeddings import EmbeddingRepository
from melone_service.store.ocr import OcrChunk, OcrChunkRepository


@dataclass(frozen=True, slots=True)
class EmbeddingIndexingResult:
    model: str
    dimension: int
    indexed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    total_ocr_chunks: int = 0
    embedded_chunks: int = 0
    last_error_type: str | None = None
    last_error_message: str | None = None


class EmbeddingIndexer:
    def __init__(
        self,
        *,
        repository: EmbeddingRepository,
        ocr_repository: OcrChunkRepository,
        model: EmbeddingModel,
        batch_size: int,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("embedding batch size must be greater than zero")

        self.repository = repository
        self.ocr_repository = ocr_repository
        self.model = model
        self.batch_size = batch_size

    def index_once(self) -> EmbeddingIndexingResult:
        info = self.model.info
        chunks = self.repository.list_missing_or_stale_ocr_chunks(
            model=info.model,
            dimension=info.dimension,
            limit=self.batch_size,
        )
        total_chunks = self.ocr_repository.count_chunks()
        if not chunks:
            return self._result(total_ocr_chunks=total_chunks)

        encoded: list[tuple[OcrChunk, EmbeddingVector]] = []
        skipped_count = 0
        for chunk in chunks:
            if not chunk.text.strip():
                skipped_count += 1
                continue

            try:
                encoded.append((chunk, self.model.encode_document(chunk.text)))
            except Exception as exc:
                return self._error_result(
                    exc,
                    skipped_count=skipped_count,
                    total_ocr_chunks=total_chunks,
                )

        try:
            with self.repository.connection:
                for chunk, embedding in encoded:
                    self.repository.upsert_chunk_embedding(
                        chunk_id=chunk.id,
                        model=info.model,
                        dimension=info.dimension,
                        text_hash=chunk.text_hash,
                        embedding=embedding,
                    )
        except Exception as exc:
            return self._error_result(
                exc,
                skipped_count=skipped_count,
                total_ocr_chunks=total_chunks,
            )

        return self._result(
            indexed_count=len(encoded),
            skipped_count=skipped_count,
            total_ocr_chunks=total_chunks,
        )

    def _result(
        self,
        *,
        indexed_count: int = 0,
        skipped_count: int = 0,
        error_count: int = 0,
        total_ocr_chunks: int,
        last_error_type: str | None = None,
        last_error_message: str | None = None,
    ) -> EmbeddingIndexingResult:
        info = self.model.info
        return EmbeddingIndexingResult(
            model=info.model,
            dimension=info.dimension,
            indexed_count=indexed_count,
            skipped_count=skipped_count,
            error_count=error_count,
            total_ocr_chunks=total_ocr_chunks,
            embedded_chunks=self.repository.count_current_chunk_embeddings(
                model=info.model,
                dimension=info.dimension,
            ),
            last_error_type=last_error_type,
            last_error_message=last_error_message,
        )

    def _error_result(
        self,
        exc: Exception,
        *,
        skipped_count: int,
        total_ocr_chunks: int,
    ) -> EmbeddingIndexingResult:
        return self._result(
            skipped_count=skipped_count,
            error_count=1,
            total_ocr_chunks=total_ocr_chunks,
            last_error_type=exc.__class__.__name__,
            last_error_message=str(exc),
        )
