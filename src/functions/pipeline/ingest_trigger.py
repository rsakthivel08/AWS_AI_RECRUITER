"""S3 ObjectCreated trigger: creates the initial record and starts the workflow.

Receiving events on:
  uploads/{sub}/resumes/{filename}          -> candidate record, ingestion workflow
  uploads/{sub}/jobs/{filename}             -> job record (status PARSING), JD parse workflow
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import boto3

from src.shared.keys import DEFAULT_RUBRIC
from src.shared.logging import get_logger, new_correlation_id
from src.shared.repo import Repo
from src.shared.settings import load_settings

import time

LOG = get_logger("ingest-trigger")


def handler(event: dict, context: Any) -> dict:
    correlation_id = new_correlation_id()
    settings = load_settings()
    sfn = boto3.client("stepfunctions", region_name=settings.aws_region)
    repo = Repo(settings.table_name, region=settings.aws_region)
    started = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        sub = _sub_from_key(key)
        if not sub:
            LOG.error("unrecognised key, skipping", extra={"extra_fields": {"key": key}})
            continue

        parts = key.split("/")
        # Path layout: uploads/{sub}/{kind}/{resource_id}/{filename} or uploads/{sub}/{kind}/{filename}
        filename = parts[-1] if parts else os.path.basename(key)

        if "/jobs/" in key:
            existing_job_id = parts[3] if len(parts) >= 5 and parts[3].startswith("j_") else None
            job_id = existing_job_id or f"j_{uuid.uuid4().hex[:12]}"
            if existing_job_id:
                repo.update_job(sub, job_id, {"status": "PARSING", "jdS3Key": key})
            else:
                repo.create_job(
                    sub, job_id,
                    title=filename.rsplit(".", 1)[0],
                    jd_s3_key=key,
                    rubric_weights=dict(DEFAULT_RUBRIC),
                    status="PARSING",
                )
            exec_name = f"job-{job_id}-{int(time.time())}"
            sfn.start_execution(
                stateMachineArn=os.environ["JOB_MACHINE_ARN"],
                name=exec_name[:80],
                input=json.dumps({
                    "jobId": job_id, "userSub": sub,
                    "s3Key": key, "bucket": bucket, "correlationId": correlation_id,
                }),
            )
            started.append({"jobId": job_id, "userSub": sub})
        else:
            existing_cand_id = parts[3] if len(parts) >= 5 and parts[3].startswith("c_") else None
            candidate_id = existing_cand_id or f"c_{uuid.uuid4().hex[:12]}"
            if existing_cand_id:
                repo.update_candidate(sub, candidate_id, {
                    "status": "PROCESSING",
                    "s3Key": key,
                    "filename": filename,
                })
            else:
                repo.create_candidate(
                    sub, candidate_id,
                    s3_key=key, filename=filename, status="PROCESSING",
                )
            exec_name = f"cand-{candidate_id}-{int(time.time())}"
            sfn.start_execution(
                stateMachineArn=os.environ["INGEST_MACHINE_ARN"],
                name=exec_name[:80],
                input=json.dumps({
                    "candidateId": candidate_id, "userSub": sub,
                    "s3Key": key, "bucket": bucket, "correlationId": correlation_id,
                }),
            )
            started.append({"candidateId": candidate_id, "userSub": sub})
    return {"started": started, "correlationId": correlation_id}


def _sub_from_key(key: str) -> str | None:
    """Key layout: uploads/{sub}/resumes|jobs/[{id}/]{file}."""
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "uploads":
        return parts[1]
    return None
