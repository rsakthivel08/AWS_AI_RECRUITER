"""Workflow step 1: validate + dedup.

- hostile-file checks: size <= 10 MB, content sniffed by magic bytes (NFR-14)
- SHA-256 over the S3 object, checked against the GSI2 dedup index (doc 3.3)
- duplicate -> candidate marked DUPLICATE and the workflow stops early
- any validation failure -> FAILED with a reason; retries only on transient S3
"""
from __future__ import annotations

import hashlib

import boto3

from src.shared.logging import get_logger
from src.shared.repo import Repo
from src.shared.settings import load_settings

LOG = get_logger("validate-dedup")
MAX_BYTES = 10 * 1024 * 1024
_PDF_MAGIC = b"%PDF-"
_DOCX_MAGIC = b"PK\x03\x04"          # DOCX is a ZIP container


class TransientError(Exception):
    """Transient S3 visibility; the state machine retries with backoff."""


def handler(event: dict, context: object) -> dict:
    settings = load_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    repo = Repo(settings.table_name, region=settings.aws_region)
    sub, cid = event["userSub"], event["candidateId"]

    try:
        obj = s3.get_object(Bucket=event["bucket"], Key=event["s3Key"])
    except s3.exceptions.NoSuchKey as exc:
        raise TransientError("object not visible yet") from exc

    body = obj["Body"].read()
    if len(body) > MAX_BYTES:
        return _fail(repo, sub, cid, "File larger than 10 MB")
    if body[:5] != _PDF_MAGIC and body[:4] != _DOCX_MAGIC:
        return _fail(repo, sub, cid, "Unsupported file type (only PDF and DOCX)")

    from src.shared.keys import cand_sk, gsi_dedup_pk

    sha256 = hashlib.sha256(body).hexdigest()
    duplicate = repo.dedup_lookup(sub, sha256)
    if duplicate and duplicate.get("candidateId") != cid:
        repo.update_candidate(sub, cid, {
            "status": "DUPLICATE",
            "duplicateOf": duplicate["candidateId"],
            "sha256": sha256,
        })
        # Prevent duplicate S3 storage by purging the redundant uploaded file
        try:
            s3.delete_object(Bucket=event["bucket"], Key=event["s3Key"])
        except Exception:
            pass
        return {"valid": False, "status": "DUPLICATE",
                "duplicateOf": duplicate["candidateId"], "sha256": sha256}

    repo.update_candidate(sub, cid, {
        "sha256": sha256,
        "sizeBytes": len(body),
        "gsi2pk": gsi_dedup_pk(sub, sha256),
        "gsi2sk": cand_sk(cid),
    })
    return {"valid": True, "sha256": sha256}


def _fail(repo: Repo, sub: str, cid: str, reason: str) -> dict:
    repo.record_failure(sub, cid, reason)
    return {"valid": False, "status": "FAILED", "failureReason": reason}
