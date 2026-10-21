"""Route implementations for candidates (FR-14, FR-15, FR-02/03, FR-13)."""
from __future__ import annotations

import boto3

from src.shared.settings import Settings


def list_candidates(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    status = query.get("status")
    limit = int(query.get("limit", 50))
    cursor = query.get("cursor")
    items, next_cursor = repo.list_candidates(sub, status=status, limit=limit, cursor=cursor)
    return {"candidates": items, "nextCursor": next_cursor}


def get_candidate(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    candidate = repo.get_candidate(sub, params["candidateId"])
    if not candidate:
        from .handler import not_found

        raise not_found()
    roles = repo.get_role_suggestion(sub, params["candidateId"])
    return {**candidate, "roles": roles or []}


def get_candidate_text(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """GET /candidates/{id}/text — the extracted resume text for free viewing."""
    cid = params["candidateId"]
    if not repo.get_candidate(sub, cid):
        from .handler import not_found

        raise not_found()
    import boto3

    s3 = boto3.client("s3", region_name=settings.aws_region)
    try:
        text = s3.get_object(Bucket=settings.text_bucket, Key=f"text/{cid}.txt")[
            "Body"].read().decode("utf-8", errors="replace")
    except s3.exceptions.NoSuchKey:
        return {"candidateId": cid, "text": "", "available": False}
    return {"candidateId": cid, "text": text, "available": True,
            "charCount": len(text)}


def delete_candidate(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """Hard delete: candidate item + cascades + S3 objects + vectors (FR-14)."""
    cid = params["candidateId"]
    if not repo.get_candidate(sub, cid):
        from .handler import not_found

        raise not_found()
    deleted_items = repo.delete_candidate_cascade(sub, cid)
    _purge_s3(sub, cid, settings)
    _purge_vectors(sub, cid, settings)
    return {"deleted": True, "candidateId": cid, "dynamoItemsDeleted": deleted_items}


def _purge_s3(sub: str, cid: str, settings: Settings) -> None:
    s3 = boto3.client("s3", region_name=settings.aws_region)
    prefixes = [f"uploads/{sub}/resumes/{cid}/", f"text/{cid}.txt",
                f"vectors/{sub}/{cid}.json"]
    for bucket in {settings.uploads_bucket, settings.text_bucket}:
        for prefix in prefixes:
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    objects = [{"Key": o["Key"]} for o in page.get("Contents", [])]
                    if objects:
                        bucket_key_objects = {"Objects": objects, "Quiet": True}
                        s3.delete_objects(Bucket=bucket, Delete=bucket_key_objects)
            except Exception:
                pass


def _purge_vectors(sub: str, cid: str, settings: Settings) -> None:
    try:
        from src.shared.vector_store import VectorStore

        VectorStore(settings).delete_for_candidate(sub, cid)
    except Exception:
        pass


def ask_candidate(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """FR-13: grounded Q&A over one candidate, citations, missing-info aware."""
    from src.shared.ai_factory import AIFactory
    from src.shared.guards import INJECTION_PREAMBLE
    from src.shared.models import AnswerOut
    from src.shared.prompt_loader import load_prompt, render
    from src.shared.vector_store import VectorStore

    cid = params["candidateId"]
    if not repo.get_candidate(sub, cid):
        from .handler import not_found

        raise not_found()
    question = (body.get("question") or "").strip()
    if not question:
        from .handler import api_error

        raise api_error(400, "VALIDATION", "question is required")

    chunks = VectorStore(settings).query(sub, question, top_k=3, candidate_id=cid)
    if not chunks:
        return {"answer": "I have no processed resume content for this candidate yet.",
                "citations": [], "missingInfo": True}
    template, _ = load_prompt("answer_about_candidate", "v1")
    user = render(template, question=question[:500],
                  chunk_0=chunks[0]["text"], chunk_1=chunks[1]["text"] if len(chunks) > 1 else "",
                  chunk_2=chunks[2]["text"] if len(chunks) > 2 else "")
    bedrock = AIFactory.for_settings(settings)
    answer = AnswerOut.model_validate(bedrock.converse_json(
        system=INJECTION_PREAMBLE, user=user, model_id=settings.bedrock_model_judge))
    out = answer.model_dump()
    citations = []
    for cit in out.get("citations", []):
        idx = cit.get("chunkIndex", -1)
        if 0 <= idx < len(chunks):
            citations.append({"text": chunks[idx]["text"], "chunkIndex": idx,
                              "quote": chunks[idx]["text"][:200]})
    out["citations"] = citations
    out["modelId"] = settings.bedrock_model_judge
    return out
