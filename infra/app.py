"""CDK app for the AI-Recruiter AWS rework (doc section 7.1).

Two environments (dev, prod) from one codebase:
    cdk synth -c env=dev && cdk deploy -c env=dev --all
    cdk deploy -c env=prod -c alarmEmail=you@example.com --all

Bucket, table, vector store and the pipeline Lambdas/machines live in ONE stack
(their S3-event notification would form a cross-stack reference cycle if split).
Doc §7.1's logical separation is preserved as construct IDs. Auth / API /
Frontend / Observability remain separate stacks.

Prod-only extras stay dormant on the Free-plan dev profile: WAF
(-c deployWaf=true), customer-managed KMS (-c deployCmk=true), Textract
policy (TEXT_SOURCE SSM flag) — all documented exceptions/flags.
"""
from __future__ import annotations

from aws_cdk import App
from stacks.common import EnvSettings
from stacks.storage import StorageStack          # retained for prod profile parity
from stacks.data import DataStack                # retained for prod profile parity
from stacks.auth import AuthStack
from stacks.pipeline import PipelineStack
from stacks.api import ApiStack
from stacks.frontend import FrontendStack
from stacks.observability import ObservabilityStack

app = App()
env_name = app.node.try_get_context("env") or "dev"
deploy_waf = bool(app.node.try_get_context("deployWaf")) and env_name != "dev"
alarm_email = app.node.try_get_context("alarmEmail")
stage = EnvSettings(
    env=env_name,
    deploy_waf=deploy_waf,
    deploy_cmk=bool(app.node.try_get_context("deployCmk")),
    deploy_textract=bool(app.node.try_get_context("deployTextract")),
    log_retention_days=90 if env_name == "prod" else 7,
    pitr=env_name == "prod",
    alarm_email=str(alarm_email) if alarm_email else None,
)

auth = AuthStack(app, f"ai-recruiter-{env_name}-auth", settings=stage)
pipeline = PipelineStack(app, f"ai-recruiter-{env_name}-backend", settings=stage,
                         user_pool=auth.user_pool)
api = ApiStack(app, f"ai-recruiter-{env_name}-api", settings=stage,
               api_function=pipeline.api_function, user_pool=auth.user_pool,
               app_client=auth.client)
frontend = FrontendStack(app, f"ai-recruiter-{env_name}-frontend", settings=stage,
                         deploy_waf=deploy_waf)
observability = ObservabilityStack(app, f"ai-recruiter-{env_name}-observability",
                                   settings=stage,
                                   ingesters={
                                       "ingestTrigger": pipeline.ingest_trigger,
                                       "extractText": pipeline.extract_text,
                                       "extractProfile": pipeline.extract_profile,
                                       "parseJd": pipeline.parse_jd,
                                       "scoreCandidate": pipeline.score_candidate,
                                       "finalizeRanking": pipeline.finalize_ranking,
                                       "api": pipeline.api_function,
                                   },
                                   dqs=[pipeline.validate_dlq, pipeline.score_dlq],
                                   state_machines=[pipeline.ingest_machine,
                                                   pipeline.job_machine,
                                                   pipeline.match_machine],
                                   api=None,
                                   frontend_id=getattr(getattr(frontend, "distribution", None),
                                                       "distribution_id", "") or "s3-hosting",
                                   user_pool_id=auth.user_pool.user_pool_id)

# Storage/Data stacks: instantiated only in the prod profile where provisioned
# KMS/Textract costs are accepted (documented exceptions); dev never creates them.
if env_name == "prod":
    storage = StorageStack(app, f"ai-recruiter-{env_name}-storage", settings=stage)
    DataStack(app, f"ai-recruiter-{env_name}-data", settings=stage,
              uploads_bucket=storage.uploads_bucket)

app.synth()
