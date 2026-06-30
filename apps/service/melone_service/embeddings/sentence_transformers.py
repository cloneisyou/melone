from __future__ import annotations

import importlib
import threading
from types import ModuleType
from typing import Any, Callable

from melone_service.config import ServiceConfig

from .errors import EmbeddingUnavailableError
from .model import (
    EmbeddingModelInfo,
    EmbeddingVector,
    normalize_embedding_vector,
)


SENTENCE_TRANSFORMERS_PROVIDER = "sentence-transformers"
_MODEL_CACHE_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, int], "SentenceTransformerEmbeddingModel"] = {}


class SentenceTransformerEmbeddingModel:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        module_loader: Callable[[str], ModuleType] = importlib.import_module,
    ) -> None:
        self.model_name = config.embedding_model
        self.dimension = config.embedding_dimension
        self._module_loader = module_loader
        self._model: Any | None = None

    @property
    def info(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(
            provider=SENTENCE_TRANSFORMERS_PROVIDER,
            model=self.model_name,
            dimension=self.dimension,
        )

    def encode_query(self, query: str) -> EmbeddingVector:
        model = self._load_model()
        try:
            vector = model.encode_query(query)
        except Exception as exc:
            raise EmbeddingUnavailableError(
                f"embedding query encoding failed for {self.model_name!r}: {exc}"
            ) from exc
        return normalize_embedding_vector(vector, dimension=self.dimension)

    def encode_document(self, text: str) -> EmbeddingVector:
        model = self._load_model()
        try:
            vector = model.encode_document(text)
        except Exception as exc:
            raise EmbeddingUnavailableError(
                f"embedding document encoding failed for {self.model_name!r}: {exc}"
            ) from exc
        return normalize_embedding_vector(vector, dimension=self.dimension)

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model

        try:
            module = self._module_loader("sentence_transformers")
        except ImportError as exc:
            raise EmbeddingUnavailableError(
                "sentence-transformers is not installed; install the semantic "
                "service extra before enabling semantic search"
            ) from exc

        try:
            model_class = module.SentenceTransformer
        except AttributeError as exc:
            raise EmbeddingUnavailableError(
                "sentence-transformers does not expose SentenceTransformer"
            ) from exc

        try:
            self._model = model_class(self.model_name)
        except Exception as exc:
            raise EmbeddingUnavailableError(
                f"embedding model {self.model_name!r} is unavailable: {exc}"
            ) from exc
        return self._model


def get_sentence_transformer_embedding_model(
    config: ServiceConfig,
) -> SentenceTransformerEmbeddingModel:
    """Return a process-local reusable embedding model wrapper.

    Loading embeddinggemma is slow enough to exceed the desktop RPC timeout if
    every search request creates a fresh SentenceTransformer instance. The
    wrapper is lazy, so this caches identity without loading weights until the
    first encode call actually needs them.
    """
    key = (config.embedding_model, config.embedding_dimension)
    with _MODEL_CACHE_LOCK:
        model = _MODEL_CACHE.get(key)
        if model is None:
            model = SentenceTransformerEmbeddingModel(config)
            _MODEL_CACHE[key] = model
        return model
