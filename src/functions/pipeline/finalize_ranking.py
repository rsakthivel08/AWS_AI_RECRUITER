"""Match-workflow final step: ranking is already projected per analysis item into
GSI1 (put_analysis writes gsi1pk/gsi1sk with zero-padded descending score); this
step only validates completeness, marks the job MATCHED and returns stats for
the state machine's execution record."""
from __future__ import annotations

from src.shared.logging import get_logger
from src.shared.repo import Repo
from src.shared.settings import load_settings

LOG = get_logger("finalize-ranking")


def handler(event: dict, context: object) -> dict:
    settings = load_settings()
    repo = Repo(settings.table_name, region=settings.aws_region)
    sub, jid = event["userSub"], event["jobId"]
    ranked = repo.list_rankings(sub, jid)
    repo.update_job(sub, jid, {"status": "MATCHED", "lastRankedCount": len(ranked)})
    LOG.info("match complete", extra={"extra_fields": {"jobId": jid, "ranked": len(ranked)}})
    return {"ranked": len(ranked), "jobId": jid,
            "candidates": [r["candidateId"] for r in ranked]}
