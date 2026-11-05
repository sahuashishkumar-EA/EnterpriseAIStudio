"""Knowledge: chunking, embeddings and similarity search.

Postgres: pgvector column `embedding_vec vector(768)` with an HNSW cosine index.
SQLite (local/tests): embeddings stored as JSON, cosine similarity computed in Python.

Embedding models:
  mock-embedding          deterministic hashed bag-of-words (dev/tests)
  text-embedding-005 /
  gemini-embedding-001    Vertex AI :predict (768 dimensions)
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

import httpx

from eap_core import gcp
from eap_core.db import Database, Tx, new_id, now_iso

DIM = 768


def chunk_text(text: str, max_chars: int = 1200, overlap: int = 150) -> list[str]:
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) <= max_chars:
        return [text] if text else []
    paras = re.split(r"\n\s*\n", text)
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 2 <= max_chars:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            chunks.append(cur)
        while len(p) > max_chars:
            chunks.append(p[:max_chars])
            p = p[max_chars - overlap:]
        cur = p
    if cur:
        chunks.append(cur)
    return chunks


def mock_embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0 if (h >> 10) & 1 else -1.0
        v[(h >> 20) % DIM] += 0.5
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


class Embedder:
    def __init__(self, project: str | None, location: str, transport: httpx.AsyncBaseTransport | None = None):
        self.project = project
        self.location = location
        self.client = httpx.AsyncClient(transport=transport, timeout=60)

    async def embed(self, texts: list[str], model: str, task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
        if model == "mock-embedding":
            return [mock_embed(t) for t in texts]
        project = self.project or await gcp.project_id()
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{self.location}"
               f"/publishers/google/models/{model}:predict")
        out: list[list[float]] = []
        token = await gcp.access_token()
        for i in range(0, len(texts), 5):
            body = {"instances": [{"content": t, "task_type": task} for t in texts[i:i + 5]],
                    "parameters": {"outputDimensionality": DIM, "autoTruncate": True}}
            r = await self.client.post(url, json=body, headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            out += [p["embeddings"]["values"] for p in r.json()["predictions"]]
        return out


class KnowledgeStore:
    def __init__(self, db: Database, embedder: Embedder):
        self.db = db
        self.embedder = embedder

    async def ingest(self, source: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
        model = (source.get("config") or {}).get("embedding_model", "mock-embedding")
        rows = []
        for d in documents:
            doc_id = str(d.get("id") or new_id("doc_"))
            for i, c in enumerate(chunk_text(d.get("text", ""))):
                rows.append((doc_id, i, d.get("title", ""), d.get("uri", ""), c, d.get("metadata") or {}))
        vectors = await self.embedder.embed([r[4] for r in rows], model) if rows else []
        pg = self.db.dialect == "postgres"

        def write(tx: Tx) -> None:
            for doc_id in {r[0] for r in rows}:
                tx.execute("DELETE FROM knowledge_chunks WHERE source_id = ? AND doc_id = ?", (source["id"], doc_id))
            for (doc_id, n, title, uri, content, meta), vec in zip(rows, vectors):
                cols = "id, source_id, doc_id, chunk_no, title, uri, content, metadata, embedding, created_at"
                vals: list[Any] = [new_id("chk_"), source["id"], doc_id, n, title, uri, content, json.dumps(meta),
                                   json.dumps(vec), now_iso()]
                if pg:
                    tx.execute(f"INSERT INTO knowledge_chunks ({cols}, embedding_vec) VALUES "
                               f"(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::vector)", (*vals, json.dumps(vec)))
                else:
                    tx.execute(f"INSERT INTO knowledge_chunks ({cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", vals)

        await self.db.call(write)
        return {"source": source["id"], "documents": len({r[0] for r in rows}), "chunks": len(rows)}

    async def search(self, source: dict[str, Any], query: str, top_k: int = 4, min_score: float = 0.0) -> list[dict[str, Any]]:
        if source.get("status", "active") != "active":
            raise ValueError(f"knowledge source {source.get('id')} is {source.get('status')}")
        model = (source.get("config") or {}).get("embedding_model", "mock-embedding")
        qv = (await self.embedder.embed([query], model, task="RETRIEVAL_QUERY"))[0]

        def read(tx: Tx) -> list[dict[str, Any]]:
            if self.db.dialect == "postgres":
                return tx.fetchall(
                    "SELECT id, doc_id, chunk_no, title, uri, content, 1 - (embedding_vec <=> ?::vector) AS score "
                    "FROM knowledge_chunks WHERE source_id = ? ORDER BY embedding_vec <=> ?::vector LIMIT ?",
                    (json.dumps(qv), source["id"], json.dumps(qv), top_k),
                )
            rows = tx.fetchall(
                "SELECT id, doc_id, chunk_no, title, uri, content, embedding FROM knowledge_chunks WHERE source_id = ?",
                (source["id"],),
            )
            for r in rows:
                r["score"] = cosine(qv, json.loads(r.pop("embedding")))
            rows.sort(key=lambda r: -r["score"])
            return rows[:top_k]

        return [r for r in await self.db.call(read) if r["score"] >= min_score]
