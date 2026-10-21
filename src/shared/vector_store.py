"""Per-user, per-candidate vector memory.

Primary: Amazon S3 Vectors (GA in ap-south-1) via the `s3vectors` client with
metadata filters (ownerSub, candidateId). Fallback (doc section 3.4): embeddings
+ chunk text stored as one JSON object per candidate in S3, queried with
in-Lambda cosine — fine at demo scale. The class auto-detects S3 Vectors
availability and remembers the decision per invocation.
"""
from __future__ import annotations

import json
import math
from typing import Any

import boto3

from .chunking import chunk_text
from .logging import get_logger

LOG = get_logger("vectors")

_VECTORS_CLIENT_OK: bool | None = None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


class VectorStore:
    def __init__(self, settings: Any, s3: Any = None):
        self.settings = settings
        self._s3 = s3 if s3 is not None else boto3.client("s3", region_name=settings.aws_region)
        self._svectors: Any = None
        global _VECTORS_CLIENT_OK
        if _VECTORS_CLIENT_OK is None:
            try:
                self._svectors = boto3.client("s3vectors", region_name=settings.aws_region)
                _VECTORS_CLIENT_OK = True
            except Exception:
                LOG.warning("s3vectors client unavailable; using S3+cosine fallback")
                _VECTORS_CLIENT_OK = False
        elif _VECTORS_CLIENT_OK:
            self._svectors = boto3.client("s3vectors", region_name=settings.aws_region)

    # ---------- write side --------------------------------------------------------

    def upsert(self, sub: str, cid: str, text: str) -> int:
        """Chunk + embed a resume (or any doc) and store with owner metadata."""
        chunks = chunk_text(text)
        if not chunks:
            return 0
        vectors = BedrockEmbed(self.settings).embed(chunks)
        if self._svectors is not None:
            try:
                for idx, (chunk, vec) in enumerate(zip(chunks, vectors)):
                    self._svectors.put_vectors(
                        vectorBucketName=self.settings.vector_bucket,
                        indexName=self.settings.vector_index,
                        vectors=[{
                            "key": f"{sub}/{cid}/{idx}",
                            "data": {"float32": vec},
                            "metadata": {
                                "ownerSub": sub, "candidateId": cid,
                                "chunkIndex": idx, "chunkText": chunk[:2000],
                            },
                        }],
                    )
                return len(chunks)
            except Exception as exc:
                LOG.warning("S3 Vectors put failed; falling back", extra={"extra_fields": {
                    "error": type(exc).__name__}})
        # ---- fallback: one JSON object per candidate ----
        self._s3.put_object(
            Bucket=self.settings.text_bucket,
            Key=f"vectors/{sub}/{cid}.json",
            Body=json.dumps({"chunks": chunks, "vectors": vectors}).encode(),
            ServerSideEncryption="aws:kms" if False else "AES256",
        )
        return len(chunks)

    # ---------- read side ----------------------------------------------------------

    def query(self, sub: str, text: str, top_k: int = 3,
              candidate_id: str | None = None) -> list[dict]:
        """Top-k most similar chunks for the user, optionally scoped to one candidate."""
        qvec = BedrockEmbed(self.settings).embed([text])[0]
        if self._svectors is not None:
            try:
                kwargs: dict[str, Any] = {
                    "vectorBucketName": self.settings.vector_bucket,
                    "indexName": self.settings.vector_index,
                    "topK": top_k,
                    "queryVector": {"float32": qvec},
                    "metadataFilter": {"ownerSub": sub},
                }
                if candidate_id:
                    kwargs["metadataFilter"] = {"ownerSub": sub, "candidateId": candidate_id}
                resp = self._svectors.query_vectors(**kwargs)
                return [{
                    "text": v["metadata"].get("chunkText", ""),
                    "score": float(v.get("distance", 0.0)) if "distance" in v else _rank_to_score(v),
                    "candidateId": v["metadata"].get("candidateId", ""),
                    "chunkIndex": int(v["metadata"].get("chunkIndex", -1)),
                } for v in resp.get("vectors", [])]
            except Exception as exc:
                LOG.warning("S3 Vectors query failed; falling back", extra={"extra_fields": {
                    "error": type(exc).__name__}})
        # ---- fallback: load per-candidate JSON objects and cosine in Lambda ----
        scores: list[dict] = []
        if candidate_id:
            keys = [f"vectors/{sub}/{candidate_id}.json"]
        else:
            keys = self._list_user_vector_keys(sub)
        for key in keys:
            try:
                obj = json.loads(self._s3.get_object(
                    Bucket=self.settings.text_bucket, Key=key)["Body"].read())
            except self._s3.exceptions.NoSuchKey:
                continue
            cid = key.rsplit("/", 1)[-1].removesuffix(".json")
            for idx, vec in enumerate(obj["vectors"]):
                scores.append({
                    "text": obj["chunks"][idx], "score": _cosine(qvec, vec),
                    "candidateId": cid, "chunkIndex": idx,
                })
        scores.sort(key=lambda s: -s["score"])
        return scores[:top_k]

    def delete_for_candidate(self, sub: str, cid: str) -> None:
        if self._svectors is not None:
            try:
                hits = self.query(sub, "", top_k=1000, candidate_id=cid)  # enumerate keys
                for h in hits:
                    self._svectors.delete_vectors(
                        vectorBucketName=self.settings.vector_bucket,
                        indexName=self.settings.vector_index,
                        keys=[f"{sub}/{cid}/{h['chunkIndex']}"],
                    )
            except Exception:
                pass
        try:
            self._s3.delete_object(
                Bucket=self.settings.text_bucket, Key=f"vectors/{sub}/{cid}.json")
        except Exception:
            pass

    def _list_user_vector_keys(self, sub: str) -> list[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(
                Bucket=self.settings.text_bucket, Prefix=f"vectors/{sub}/"):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys


def _rank_to_score(v: dict) -> float:
    """S3 Vectors returns distance/similarity depending on config; normalise."""
    for key in ("similarity", "score", "distance"):
        if key in v:
            raw = float(v[key])
            return raw if raw <= 1.0 else math.log(1 + raw)
    return 0.0


class BedrockEmbed:
    """Thin embed adapter so VectorStore does not import the full client cycle.

    Despite the legacy name it is provider-agnostic: it routes through the
    AI factory, so ``AI_PROVIDER=gemini`` yields Gemini embeddings.
    """

    def __init__(self, settings: Any):
        from .ai_factory import AIFactory

        self._client = AIFactory.for_settings(settings)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._client.embed(texts)
