"""Shared per-environment settings from CDK context flags."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EnvSettings:
    env: str
    deploy_waf: bool          # prod-only; documented dev exception NFR-12
    deploy_cmk: bool          # prod-only; documented dev exception NFR-08
    deploy_textract: bool     # dormant Textract path (plan deviation section 2)
    log_retention_days: int   # 7 dev / 90 prod (doc section 7.3)
    pitr: bool
    alarm_email: str | None   # alarms only in prod

    def knob(self, name: str, default: str) -> str:
        return {"dev": default, "prod": default}[self.env] or default
