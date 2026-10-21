"""Route implementations: presigned uploads, pasted-text extract (prototype parity).

Presign creates the record up front (candidate/job) so the UI can list it
immediately; the S3 event trigger only starts the workflow. Presigned URLs are
5-minute, single-key, et al. (NFR-09).
"""
from __future__ import annotations

import fnmatch
import uuid

import boto3

from src.shared.guards import INJECTION_PREAMBLE, wrap_untrusted
from src.shared.keys import DEFAULT_RUBRIC
from src.shared.logging import get_logger
from src.shared.models import Profile
from src.shared.prompt_loader import load_prompt, record_version, render
from src.shared.settings import Settings

LOG = get_logger("impl-ingest")

ALLOWED = {"resume": ("pdf", "docx"), "job": ("pdf", "docx", "txt")}
MAX_BYTES = 10 * 1024 * 1024
PRESIGN_TTL = 300


def presign(sub: str, body: dict, repo, settings: Settings, **kw) -> dict:
    kind = body.get("kind", "resume")
    filename = (body.get("filename") or "unnamed").strip()
    ext = fnmatch.fnmatch(filename, "*.*") and filename.rsplit(".", 1)[-1].lower() or ""
    if ext not in ALLOWED.get(kind, ()):
        from .handler import api_error

        raise api_error(400, "UNSUPPORTED_FILE_TYPE",
                        f"Allowed types for {kind}: {', '.join(ALLOWED[kind])}")
    if int(body.get("sizeBytes") or 0) > MAX_BYTES:
        from .handler import api_error

        raise api_error(400, "TOO_LARGE", "Files are capped at 10 MB")

    sha256 = (body.get("sha256") or "").strip().lower()
    if sha256 and kind == "resume":
        duplicate = repo.dedup_lookup(sub, sha256)
        if duplicate:
            return {
                "already_exists": True,
                "candidateId": duplicate["candidateId"],
                "filename": duplicate.get("filename", filename),
                "status": duplicate.get("status", "READY"),
                "message": f"Duplicate detected — this resume was already uploaded.",
            }

    folder = "resumes" if kind == "resume" else "jobs"
    id_prefix = "c_" if kind == "resume" else "j_"
    resource_id = f"{id_prefix}{uuid.uuid4().hex[:12]}"
    s3_key = f"uploads/{sub}/{folder}/{resource_id}/{filename}"

    if kind == "resume":
        repo.create_candidate(sub, resource_id, s3_key=s3_key,
                              filename=filename, status="UPLOADED")
    else:
        # JD payload fields parsed by the workflow once the file arrives.
        repo.create_job(sub, resource_id, title=f"{filename.rsplit('.', 1)[0]} (file upload)"
                        if "." in filename else "(JD upload)",
                        jd_s3_key=s3_key, rubric_weights=dict(DEFAULT_RUBRIC),
                        status="PARSING")

    s3 = boto3.client("s3", region_name=settings.aws_region,
                      endpoint_url=f"https://s3.{settings.aws_region}.amazonaws.com")
    url = s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.uploads_bucket,
            "Key": s3_key,
            "ContentType": _content_type(ext),
        },
        ExpiresIn=PRESIGN_TTL,
    )
    return {
        "kind": kind,
        "resourceId": resource_id,
        "s3Key": s3_key,
        "uploadUrl": url if not str(url).startswith("http$") else url,
        "expirySeconds": PRESIGN_TTL,
        "next": "PUT the file to uploadUrl; the pipeline starts automatically.",
    }


def _content_type(ext: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
    }.get(ext, "application/octet-stream")


def extract_text(sub: str, body: dict, repo, settings: Settings, **kw) -> dict:
    """POST /extract — prototype parity:bedrock-extract a pasted text block."""
    text = (body.get("text") or "").strip()
    if not text or len(text) < 40:
        from .handler import api_error

        raise api_error(400, "VALIDATION", " Provide at least 40 chars of text")
    from src.shared.ai_factory import AIFactory

    template, _ = load_prompt("extract_profile", "v1")
    user = render(template, resume_text=wrap_untrusted("RESUME", text[:15000]))
    client = AIFactory.for_settings(settings)
    data = client.converse_json(
        system=INJECTION_PREAMBLE, user=user, model_id=settings.bedrock_model_small)
    profile = Profile.model_validate(data)
    return {**profile.model_dump(), **record_version("extract_profile", "v1")}
