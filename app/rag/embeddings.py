from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Iterable

from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from app.config import Settings


class LocalHashEmbeddings(Embeddings):
    """Small deterministic embedding for local/dev retrieval.

    This is intentionally dependency-free and stable for tests. It is not meant
    to beat real embedding models, but it gives Chroma semantic-ish lexical
    recall without requiring an external embedding API.
    """

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = self._tokens(text)
        counts = Counter(tokens)
        for token, count in counts.items():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[idx] += sign * float(count)
        norm = math.sqrt(sum(v * v for v in vector))
        if norm <= 0.0:
            return vector
        return [v / norm for v in vector]

    def _tokens(self, text: str) -> Iterable[str]:
        clean = str(text or "").lower()
        buffer = ""
        for ch in clean:
            if "\u4e00" <= ch <= "\u9fff":
                if buffer:
                    yield buffer
                    buffer = ""
                yield ch
            elif ch.isalnum():
                buffer += ch
            else:
                if buffer:
                    yield buffer
                    buffer = ""
        if buffer:
            yield buffer


class FastEmbedEmbeddings(Embeddings):
    """Local ONNX embedding model through fastembed.

    The default model is BAAI/bge-small-zh-v1.5 (~90MB), which is small enough
    for a local game backend and has much better Chinese recall than hash
    embeddings.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        cache_dir: str = "data/models/fastembed",
        threads: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threads = threads
        self._model = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        clean_texts = [str(text or "") for text in texts]
        return [self._to_float_list(vector) for vector in self._embedder().embed(clean_texts)]

    def embed_query(self, text: str) -> list[float]:
        vectors = list(self._embedder().query_embed(str(text or "")))
        if vectors:
            return self._to_float_list(vectors[0])
        return []

    def _embedder(self):
        if self._model is None:
            from fastembed import TextEmbedding

            kwargs = {}
            local_model_dir = self._local_model_dir()
            if local_model_dir is not None:
                kwargs["specific_model_path"] = str(local_model_dir)
                kwargs["local_files_only"] = True
            self._model = TextEmbedding(
                model_name=self.model_name,
                cache_dir=self.cache_dir,
                threads=self.threads,
                lazy_load=False,
                **kwargs,
            )
        return self._model

    def _local_model_dir(self):
        from pathlib import Path

        last_name = self.model_name.split("/")[-1]
        candidates = [
            Path(self.cache_dir) / f"fast-{last_name}",
            Path(self.cache_dir) / last_name,
        ]
        for candidate in candidates:
            if candidate.exists() and any(candidate.iterdir()):
                return candidate
        return None

    def _to_float_list(self, vector) -> list[float]:
        if hasattr(vector, "tolist"):
            return [float(v) for v in vector.tolist()]
        return [float(v) for v in vector]


def build_embeddings(settings: Settings | None = None) -> Embeddings:
    resolved = settings or Settings()
    provider = str(getattr(resolved, "embedding_provider", "local_hash") or "local_hash").strip().lower()
    if provider in {"fastembed", "local", "onnx"}:
        return FastEmbedEmbeddings(
            model_name=resolved.embedding_model or "BAAI/bge-small-zh-v1.5",
            cache_dir=resolved.embedding_cache_dir or "data/models/fastembed",
        )
    if provider in {"openai", "openai_compatible", "api"}:
        kwargs: dict[str, str] = {"model": resolved.embedding_model or "text-embedding-3-small"}
        api_key = str(resolved.embedding_api_key or resolved.api_key or "not-needed").strip()
        base_url = str(resolved.embedding_base_url or "").strip().rstrip("/")
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIEmbeddings(**kwargs)
    return LocalHashEmbeddings()
