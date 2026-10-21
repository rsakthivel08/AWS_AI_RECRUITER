"""Route implementations for jobs (FR-04, FR-05–09, FR-14)."""
from __future__ import annotations

import csv
import io
import json
import os
import uuid

import boto3

from src.shared.keys import DEFAULT_RUBRIC, score_band, validate_rubric
from src.shared.logging import get_logger
from src.shared.models import JobRequirements
from src.shared.settings import Settings

LOG = get_logger("impl-jobs")

M = "MATCH MACHINE"


def create_job(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """POST /jobs from PASTED text: returns 202, JD parse runs in the workflow."""
    if not body.get("jdText"):
        from .handler import api_error

        raise api_error(400, "VALIDATION", "jdText is required (file JDs arrive via S3)")
    jid = f"j_{uuid.uuid4().hex[:12]}"
    repo.create_job(sub, jid, title=body.get("title") or "(untitled job)",
                    jd_s3_key=None, rubric_weights=dict(DEFAULT_RUBRIC),
                    status="PARSING")
    # Persist the raw JD text so GET /jobs/{id} can always show what the
    # recruiter pasted (Phase 1 of the job-match restructure plan).
    repo.update_job(sub, jid, {"jdText": str(body["jdText"])[:60000]})
    execution_arn = _start_job_parse(sub, jid, settings, text=body["jdText"])
    return {"jobId": jid, "status": "PARSING", "executionArn": execution_arn,
            "poll": f"/jobs/{jid}"}, 202


def _start_job_parse(sub: str, jid: str, settings: Settings, text: str | None = None) -> str:
    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    payload: dict = {"jobId": jid, "userSub": sub}
    if text:
        payload["jdText"] = text[:60000]
    machine_arn = os.environ["JOB_MACHINE_ARN"]
    execution = sfn.start_execution(stateMachineArn=machine_arn,
                                    name=f"jd-{jid}",
                                    input=json.dumps(payload))
    return execution["executionArn"]


def list_jobs(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    jobs, next_cursor = repo.list_jobs(sub)
    return {"jobs": jobs, "nextCursor": next_cursor}


def get_job(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    job = repo.get_job(sub, params["jobId"])
    if not job:
        from .handler import not_found

        raise not_found()
    return job


def update_job(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """PUT /jobs/{id} — recruiter edits to parsed requirements / rubric weights."""
    from .handler import api_error

    jid = params["jobId"]
    job = repo.get_job(sub, jid)
    if not job:
        from .handler import not_found

        raise not_found()
    updates: dict = {}
    if "parsedRequirements" in body:
        parsed = JobRequirements.model_validate(body["parsedRequirements"])
        updates["parsedRequirements"] = parsed.model_dump()
    if "rubricWeights" in body:
        merged = {**DEFAULT_RUBRIC, **body["rubricWeights"]}
        merged = {k: int(v) for k, v in merged.items()}
        try:
            validate_rubric(merged)
        except ValueError as exc:
            raise api_error(400, "VALIDATION", str(exc)) from exc
        updates["rubricWeights"] = merged
    if "title" in body and body["title"]:
        updates["title"] = str(body["title"])[:120]
    if not updates:
        raise api_error(400, "VALIDATION", "nothing to update")
    return repo.update_job(sub, jid, updates)


def delete_job(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    jid = params["jobId"]
    if not repo.get_job(sub, jid):
        from .handler import not_found

        raise not_found()
    deleted = repo.delete_job_cascade(sub, jid)
    return {"deleted": True, "jobId": jid, "dynamoItemsDeleted": deleted}


def start_match(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """POST /jobs/{id}/match — 202; a Map state scores chosen/ready candidates."""
    from .handler import api_error, not_found

    jid = params["jobId"]
    job = repo.get_job(sub, jid)
    if not job:
        raise not_found()
    if job.get("status") == "PARSING":
        raise api_error(409, "JOB_NOT_READY", "JD is still parsing; retry shortly")

    requested = body.get("candidateIds")
    if requested:
        candidates = [repo.get_candidate(sub, cid) for cid in requested]
        missing = [cid for cid, got in zip(requested, candidates) if not got]
        if missing:
            raise api_error(404, "NOT_FOUND", f"Candidates not found: {missing}")
        candidates = [c for c in candidates if c]
    else:
        items, _cursor = repo.list_candidates(sub, status="READY", limit=200)
        candidates = items
    if not candidates:
        raise api_error(409, "NO_CANDIDATES", "No READY candidates to match")

    usage = repo.increment_daily_usage(sub)
    limit = settings.int_("daily_match_limit", 500)
    if usage > limit:
        raise api_error(429, "DAILY_LIMIT", f"Daily match limit ({limit}) reached")

    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    # DynamoDB stores numbers as Decimal — the SFN input must be strict JSON,
    # so rubric weights are coerced to plain ints before serialisation.
    rubric = {k: int(v) for k, v in (job.get("rubricWeights") or dict(DEFAULT_RUBRIC)).items()}
    execution = sfn.start_execution(
        stateMachineArn=_match_machine_arn(settings),
        name=f"match-{jid}-{usage}",
        input=__import__("json").dumps({
            "jobId": jid, "userSub": sub,
            "candidateIds": [c["candidateId"] for c in candidates],
            "rubricWeights": rubric,
        }),
    )
    return {"jobId": jid, "status": "MATCHING", "executionArn": execution["executionArn"],
            "candidateCount": len(candidates)}, 202


def _match_machine_arn(settings: Settings) -> str:
    import os

    return os.environ["MATCH_MACHINE_ARN"]


def rankings(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    jid = params["jobId"]
    if not repo.get_job(sub, jid):
        from .handler import not_found

        raise not_found()
    min_score = int(query.get("minScore", 0))
    rows = repo.list_rankings(sub, jid, min_score=min_score)
    # Join stored candidate names so the UI shows people, not bare ids.
    names = {}
    analysis_exists: dict[str, bool] = {}
    for row in rows:
        cid = row.get("candidateId", "")
        cand = repo.get_candidate(sub, cid) if cid else None
        names[cid] = ((cand or {}).get("profile") or {}).get("name") \
            or (cand or {}).get("filename") or cid
        # Check whether a scored analysis record exists so the UI can enable
        # the Compare checkbox and Analysis drawer only for fully-analysed candidates.
        analysis_exists[cid] = bool(repo.get_analysis(sub, jid, cid))
    return {
        "jobId": jid,
        "rankings": [{
            **{k: row.get(k) for k in ("candidateId", "totalScore")},
            "name": names.get(row.get("candidateId", ""), ""),
            "band": score_band(int(row.get("totalScore", 0))),
            "flags": row.get("flags", []),
            "analysisExists": analysis_exists.get(row.get("candidateId", ""), False),
        } for row in rows],
    }


def get_analysis(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    analysis = repo.get_analysis(sub, params["jobId"], params["candidateId"])
    if not analysis:
        from .handler import not_found

        raise not_found()
    return analysis


def export_csv(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    jid = params["jobId"]
    if not repo.get_job(sub, jid):
        from .handler import not_found

        raise not_found()
    rows = repo.list_rankings(sub, jid)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["rank", "candidateId", "totalScore", "band"])
    for i, row in enumerate(rows, 1):
        writer.writerow([i, row.get("candidateId"),
                         row.get("totalScore"),
                         score_band(int(row.get("totalScore", 0)))])
    s3 = boto3.client("s3", region_name=settings.aws_region)
    key = f"exports/{sub}/{jid}.csv"
    s3.put_object(Bucket=settings.exports_bucket, Key=key, Body=buf.getvalue().encode(),
                  ServerSideEncryption="AES256")
    url = s3.generate_presigned_url("get_object", Params={
        "Bucket": settings.exports_bucket, "Key": key}, ExpiresIn=300)
    return {"downloadUrl": url, "expirySeconds": 300, "rows": len(rows)}
