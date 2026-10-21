"""Workflow step: parse a job description into structured requirements (FR-04).

Runs in the JD-parse workflow (file JDs) and is called directly by the API for
pasted-text JDs. The parsed requirements are editable by the recruiter (PUT
/jobs/{id}); edited values are what later scoring uses.
"""
from __future__ import annotations

from src.shared.logging import get_logger
from src.shared.models import JobRequirements
from src.shared.prompt_loader import load_prompt, record_version, render
from src.shared.repo import Repo
from src.shared.settings import load_settings

LOG = get_logger("parse-jd")
PROMPT_NAME = "parse_jd"


def parse_jd_text(settings: object, jd_text: str, user_sub: str) -> tuple[dict, dict]:
    """Returns ({parsed requirements dict}, {prompt logging connection metadata})."""
    from src.shared.ai_factory import AIFactory
    from src.shared.guards import INJECTION_PREAMBLE, wrap_untrusted

    template, schema = load_prompt(PROMPT_NAME, "v1")
    system = INJECTION_PREAMBLE
    user = render(template, jd_text=wrap_untrusted("JOB_DESCRIPTION", jd_text))
    # The template already renders the labelled block; strip its duplicated
    # document placeholder markers when it interpolated.
    bedrock = AIFactory.for_settings(settings)
    data = bedrock.converse_json(
        system=system,
        user=user,
        model_id=settings.bedrock_model_small,
    )
    requirements = JobRequirements.model_validate(data)
    return requirements.model_dump(), record_version(PROMPT_NAME, "v1")


def handler(event: dict, context: object) -> dict:
    """Step Functions entry: event carries jobId, userSub and either s3Key+bucket or jdText."""
    settings = load_settings()
    repo = Repo(settings.table_name, region=settings.aws_region)
    sub, jid = event["userSub"], event["jobId"]
    if "jdText" in event:  # pasted-text JD (POST /jobs /parse workflow)
        jd_text = event["jdText"]
    else:
        import boto3

        s3 = boto3.client("s3", region_name=settings.aws_region)
        jd_text = s3.get_object(Bucket=event["bucket"], Key=event["s3Key"])["Body"].read().decode(
            "utf-8", errors="replace")
    try:
        parsed, meta = parse_jd_text(settings, jd_text, sub)
        updates: dict = {**parsed, **meta, "status": "READY"}
        # The AI-extracted title names the job (see parse_jd v1 rule 1); only
        # use it when create_job could not know one (its "(untitled job)"
        # placeholder) — an explicit recruiter title must never be overwritten.
        extracted_title = str(parsed.get("title") or "").strip()
        if extracted_title:
            updates["title"] = extracted_title[:120]
        repo.update_job(sub, jid, updates)
        return {"parsed": True, "requirements": parsed}
    except Exception as exc:  # noqa: BLE001
        repo.update_job(sub, jid, {"status": "FAILED", "failureReason": str(exc)[:500]})
        raise
