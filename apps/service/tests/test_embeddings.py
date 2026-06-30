from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from melone_service.embeddings import (
    EmbeddingUnavailableError,
    FakeEmbeddingModel,
    SentenceTransformerEmbeddingModel,
)
from melone_service.embeddings.model import normalize_embedding_vector


def test_normalize_embedding_vector_returns_normalized_float32():
    vector = normalize_embedding_vector([3.0, 4.0], dimension=2)

    assert vector.dtype == np.float32
    assert vector.shape == (2,)
    np.testing.assert_allclose(vector, np.asarray([0.6, 0.8], dtype=np.float32))
    np.testing.assert_allclose(np.linalg.norm(vector), 1.0)


def test_normalize_embedding_vector_truncates_and_renormalizes():
    vector = normalize_embedding_vector([3.0, 4.0, 12.0], dimension=2)

    assert vector.dtype == np.float32
    assert vector.shape == (2,)
    np.testing.assert_allclose(vector, np.asarray([0.6, 0.8], dtype=np.float32))
    np.testing.assert_allclose(np.linalg.norm(vector), 1.0)


@pytest.mark.parametrize(
    ("vector", "dimension", "message"),
    [
        ([[1.0, 2.0]], 2, "one-dimensional"),
        ([1.0, float("nan")], 2, "finite"),
        ([1.0], 2, "expected at least 2"),
        ([0.0, 0.0], 2, "norm"),
        ([0.0, 0.0, 1.0], 2, "truncated"),
        (["not-a-number"], 1, "numeric"),
    ],
)
def test_normalize_embedding_vector_rejects_malformed_vectors(
    vector,
    dimension,
    message,
):
    with pytest.raises(ValueError, match=message):
        normalize_embedding_vector(vector, dimension=dimension)


def test_sentence_transformer_adapter_is_lazy_and_uses_model_paths():
    calls: list[tuple[str, str]] = []

    class StubSentenceTransformer:
        def __init__(self, model_name: str) -> None:
            calls.append(("load", model_name))

        def encode_query(self, query: str) -> list[float]:
            calls.append(("query", query))
            return [3.0, 4.0, 12.0]

        def encode_document(self, text: str) -> list[float]:
            calls.append(("document", text))
            return [0.0, 5.0, 12.0]

    def load_module(name: str) -> SimpleNamespace:
        calls.append(("import", name))
        return SimpleNamespace(SentenceTransformer=StubSentenceTransformer)

    adapter = SentenceTransformerEmbeddingModel(
        _embedding_config(dimension=2),
        module_loader=load_module,
    )

    assert calls == []

    query_vector = adapter.encode_query("search query")
    document_vector = adapter.encode_document("ocr chunk text")

    assert calls == [
        ("import", "sentence_transformers"),
        ("load", "test-embedding-model"),
        ("query", "search query"),
        ("document", "ocr chunk text"),
    ]
    np.testing.assert_allclose(
        query_vector,
        np.asarray([0.6, 0.8], dtype=np.float32),
    )
    np.testing.assert_allclose(
        document_vector,
        np.asarray([0.0, 1.0], dtype=np.float32),
    )


def test_sentence_transformer_adapter_raises_unavailable_for_missing_dependency():
    def load_missing_module(name: str) -> SimpleNamespace:
        raise ModuleNotFoundError(f"No module named {name!r}")

    adapter = SentenceTransformerEmbeddingModel(
        _embedding_config(),
        module_loader=load_missing_module,
    )

    with pytest.raises(EmbeddingUnavailableError, match="sentence-transformers"):
        adapter.encode_query("search query")


def test_fake_embedding_model_is_deterministic_and_separates_paths():
    model = FakeEmbeddingModel(dimension=4)

    first_query = model.encode_query("same text")
    second_query = model.encode_query("same text")
    document = model.encode_document("same text")

    np.testing.assert_array_equal(first_query, second_query)
    assert not np.array_equal(first_query, document)
    np.testing.assert_allclose(np.linalg.norm(first_query), 1.0)
    np.testing.assert_allclose(np.linalg.norm(document), 1.0)
    assert model.query_calls == ["same text", "same text"]
    assert model.document_calls == ["same text"]


def test_fake_embedding_model_supports_separate_query_document_overrides():
    model = FakeEmbeddingModel(
        dimension=2,
        query_vectors={"same text": [1.0, 0.0]},
        document_vectors={"same text": [0.0, 1.0]},
    )

    np.testing.assert_allclose(
        model.encode_query("same text"),
        np.asarray([1.0, 0.0], dtype=np.float32),
    )
    np.testing.assert_allclose(
        model.encode_document("same text"),
        np.asarray([0.0, 1.0], dtype=np.float32),
    )


def _embedding_config(
    *,
    model: str = "test-embedding-model",
    dimension: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        embedding_model=model,
        embedding_dimension=dimension,
    )
