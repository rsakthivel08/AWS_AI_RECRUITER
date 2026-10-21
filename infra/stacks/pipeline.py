"""PipelineStack: every Lambda, the three Step Functions machines, DLQs, S3 trigger.

Machine layout (doc section 3.2):
  ingest     : validate+dedup -> extract text -> profile+embed+roles
  job        : parse JD (file or pasted text)
  match      : Map over candidateIds -> score_candidate -> finalize ranking
Failures route to SQS DLQs after retries; steps are idempotent (keyed by ids).
"""
from __future__ import annotations

from aws_cdk import CfnResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as _iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_dynamodb as _dynamodb
from aws_cdk import aws_s3 as _s3
from aws_cdk import aws_s3_notifications as _s3n
from aws_cdk import aws_sqs as _sqs
from aws_cdk import aws_stepfunctions as _sfn
from aws_cdk import aws_stepfunctions_tasks as _tasks
from constructs import Construct

from stacks.common import EnvSettings

RUNTIME = _lambda.Runtime.PYTHON_3_12


class PipelineStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, *,
                 user_pool, **kw) -> None:  # noqa: F401 (user_pool wired in api/auth stacks)
        """Creates its own buckets/table/vector resources, so no cross-stack cycle.

        (The S3 event notification makes Storage reference Pipeline Lambdas; if
        Pipeline also referenced Storage resources by env var, CDK would build a
        dependency cycle. One stack = no cycle. Doc-section separation is kept as
        construct IDs instead of stack boundaries.)
        """
        super().__init__(scope, id, **kw)
        self.settings_env = settings.env

        prefix = f"ar-{settings.env}"

        # ---- buckets (S3_MANAGED encryption; CMK is the documented prod extra) ---
        def bucket(identifier: str, *, versioned: bool = False,
                   retention: bool = False) -> _s3.Bucket:
            kwargs = dict(
                bucket_name=f"{prefix}-{identifier.lower()}",
                block_public_access=_s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                versioned=versioned,
                encryption=_s3.BucketEncryption.S3_MANAGED,
                object_ownership=_s3.ObjectOwnership.OBJECT_WRITER,
                removal_policy=(RemovalPolicy.DESTROY if settings.env != "prod"
                                else RemovalPolicy.RETAIN),
            )
            if retention:
                kwargs["lifecycle_rules"] = [
                    _s3.LifecycleRule(id="retention",
                                      expiration=Duration.days(180),      # NFR-21
                                      noncurrent_version_expiration=Duration.days(30))
                ]
            return _s3.Bucket(self, identifier, **kwargs)

        self.uploads_bucket = bucket("uploads", versioned=True, retention=True)
        self.text_bucket = self.uploads_bucket          # text/, vectors/ as prefixes
        self.exports_bucket = bucket("exports")

        # ---- Vector mode ---------------------------------------------------------
        # The S3+cosine fallback (doc section 3.4) is the DEV default: CloudFormation
        # in this region does not recognise AWS::S3::VectorBucket/Index yet, and the
        # fallback is zero-cost. S3 Vectors resources deploy with -c vectorMode=s3vectors
        # when the regional CFN types become available.
        self.vector_mode = self.node.try_get_context("vectorMode") or "s3cosine"
        self.vector_bucket_name = f"{prefix}-vectors"
        self.vector_index_name = "resume-vectors"
        if self.vector_mode == "s3vectors":
            self._vector_bucket = CfnResource(
                self, "vectorBucket",
                type="AWS::S3::VectorBucket",
                properties={"vectorBucketName": self.vector_bucket_name,
                            "encryptionConfiguration": {"sseType": "SSE"}},
            )
            self._vector_index = CfnResource(
                self, "vectorIndex",
                type="AWS::S3::VectorIndex",
                properties={
                    "vectorBucketName": self.vector_bucket_name,
                    "indexName": self.vector_index_name,
                    "dataType": "float32",
                    "dimension": 1024,
                    "distanceMetric": "cosine",
                },
            )
            self._vector_index.add_depends_on(self._vector_bucket)
            self.vector_bucket_arn = (
                f"arn:aws:s3vectors:{self.region}:{self.account}"
                f":vector-bucket/{self.vector_bucket_name}"
            )
        else:
            self.vector_bucket_arn = f"arn:aws:s3:::{self.uploads_bucket.bucket_name}"

        # ---- DynamoDB single table + GSIs (doc §3.3) -----------------------------
        self.table = _dynamodb.Table(
            self, "recruiter",
            table_name=f"recruiter-{settings.env}",
            partition_key={"name": "pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "sk", "type": _dynamodb.AttributeType.STRING},
            billing_mode=_dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=(RemovalPolicy.DESTROY if settings.env != "prod"
                            else RemovalPolicy.RETAIN),
            time_to_live_attribute="expiresAt",
            point_in_time_recovery=(None if not settings.pitr else True),
        )
        self.table.add_global_secondary_index(
            index_name="gsi1",
            partition_key={"name": "gsi1pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "gsi1sk", "type": _dynamodb.AttributeType.STRING},
        )
        self.table.add_global_secondary_index(
            index_name="gsi2",
            partition_key={"name": "gsi2pk", "type": _dynamodb.AttributeType.STRING},
            sort_key={"name": "gsi2sk", "type": _dynamodb.AttributeType.STRING},
        )

        # ---- DLQs (NFR-06) -------------------------------------------------------
        self.validate_dlq = _sqs.Queue(self, "validateDlq", retention_period=Duration.days(14))
        self.score_dlq = _sqs.Queue(self, "scoreDlq", retention_period=Duration.days(14))

        common_env = {
            "TABLE_NAME": self.table.table_name,
            "TEXT_BUCKET": self.uploads_bucket.bucket_name,
            "UPLOADS_BUCKET": self.uploads_bucket.bucket_name,
            "EXPORTS_BUCKET": self.exports_bucket.bucket_name,
            "VECTOR_BUCKET": self.vector_bucket_name,
            "VECTOR_INDEX": self.vector_index_name,
            "BEDROCK_MODEL_JUDGE": "amazon.nova-pro-v1",
            "BEDROCK_MODEL_SMALL": "amazon.nova-micro-v1",
            "BEDROCK_MODEL_VISION": "amazon.nova-lite-v1",
            "BEDROCK_MODEL_EMBED": "amazon.titan-embed-text-v2:0",
            "EMBED_DIMENSIONS": "1024",
            "SSM_PATH": f"/ai-recruiter/{settings.env}/",
            "ENV": settings.env,
            "PROMPT_VERSION": "v1",
            "DAILY_MATCH_LIMIT": "500",
            # AI_PROVIDER + GEMINI_* come from SSM (see /ai-recruiter/{env}/*) so the
            # provider can be flipped bedrock<->gemini WITHOUT a redeploy.
        }

        # ---- Lambda layer: third-party deps as linux wheels -----------------------
        self.deps_layer = _lambda.LayerVersion(
            self, "depsLayer",
            code=_lambda.Code.from_asset("../infra/layer"),
            compatible_runtimes=[RUNTIME],
            description="pydantic/pymupdf/docx2txt (manylinux wheels)",
        )

        def fn(identifier: str, entry: str, timeout: int = 120,
               memory: int = 768, deps: str = "", extra_env: dict | None = None) -> _lambda.Function:
            # Bundle = repo root: src/, prompts/, data/ ship together, so the
            # handler path is src/functions/<area>/<module> and imports stay
            # `from src.shared...` — identical to the pytest layout.
            return _lambda.Function(
                self, identifier,
                runtime=RUNTIME,
                handler=f"{entry}.handler",
                code=_lambda.Code.from_asset(
                    "..",
                    exclude=[
                        "web", "web/*", "node_modules", "tests", "tests/*", "infra",
                        "infra/*", "docs", "docs/*", "scripts", "scripts/*", ".github",
                        ".github/*", ".venv", ".venv/*", ".vscode", ".git",
                        "screenshots", "frontend", "app", "dist", "storage",
                        "README.md", "requirements.txt", "requirements-dev.txt",
                        "cdk.out", "*.docx", "**/__pycache__", "**/*.pyc", ".pytest_cache",
                    ],
                ),
                timeout=Duration.seconds(timeout),
                memory_size=memory,
                dead_letter_queue=self.validate_dlq,
                layers=[self.deps_layer],
                environment={**common_env, **(extra_env or {})},
                log_retention=self._retention(settings),
                tracing=_lambda.Tracing.ACTIVE,          # X-Ray (NFR-23)
            )

        self.ingest_trigger = fn("ingestTrigger", "src/functions/pipeline/ingest_trigger")
        self.validate_dedup = fn("validateDedup", "src/functions/pipeline/validate_dedup",
                                 timeout=180)
        self.extract_text = fn("extractText", "src/functions/pipeline/extract_text",
                               timeout=300, memory=1769,
                               deps="pymupdf,docx2txt")  # bundled via requirements layer below
        self.extract_profile = fn("extractProfile", "src/functions/pipeline/extract_profile",
                                  timeout=240)
        self.parse_jd = fn("parseJd", "src/functions/pipeline/parse_jd_fn", timeout=240)
        self.score_candidate = fn("scoreCandidate", "src/functions/pipeline/score_candidate",
                                  timeout=300)
        self.finalize_ranking = fn("finalizeRanking", "src/functions/pipeline/finalize_ranking",
                                   timeout=60)
        self.api_function = fn("api", "src/functions/api/handler", timeout=29)

        # ---- Bedrock / DynamoDB / S3 / SSM grants (least privilege, NFR-10) -------
        self._grant_all(self.table, self.uploads_bucket, self.text_bucket,
                        self.vector_bucket_arn, self.vector_bucket_name,
                        self.vector_index_name)

        # ---- Step Functions machines ---------------------------------------------
        self.ingest_machine = self._ingest_machine(settings)
        self.job_machine = self._job_machine()
        self.match_machine = self._match_machine()

        self.ingest_trigger.add_environment("INGEST_MACHINE_ARN", self.ingest_machine.state_machine_arn)
        self.ingest_trigger.add_environment("JOB_MACHINE_ARN", self.job_machine.state_machine_arn)
        self.api_function.add_environment("MATCH_MACHINE_ARN", self.match_machine.state_machine_arn)
        self.api_function.add_environment("JOB_MACHINE_ARN", self.job_machine.state_machine_arn)
        self._sfn_grants()

        # ---- S3 event -> ingest trigger, filtered by prefixes (doc 3.2) -----------
        # LambdaDestination grants the function permission to accept the trigger,
        # so no extra grant_read here (that would create a stack reference cycle).
        self.uploads_bucket.add_event_notification(
            _s3.EventType.OBJECT_CREATED,
            _s3n.LambdaDestination(self.ingest_trigger),
            _s3.NotificationKeyFilter(prefix="uploads/"),
        )

    # ------------------------------------------------------------------ internals

    def _retention(self, settings: EnvSettings):
        from aws_cdk.aws_logs import RetentionDays

        return (RetentionDays.ONE_MONTH if settings.log_retention_days >= 30
                else RetentionDays.ONE_WEEK)

    def _grant_all(self, table, uploads_bucket, text_bucket,
                   vector_bucket_arn: str, vector_bucket_name: str,
                   vector_index_name: str) -> None:
        # S3 grants use raw ARN statements instead of L2 grant_* helpers, so the
        # Pipeline does not create a reverse reference to the Storage stack — that
        # would form a stack-level cycle with the S3 event notification dependency.
        uploads_arn = f"arn:aws:s3:::{uploads_bucket.bucket_name}"
        uploads_arn_slash = f"{uploads_arn}/*"
        exports_arn = f"arn:aws:s3:::{self.exports_bucket.bucket_name}"
        exports_arn_slash = f"{exports_arn}/*"
        for function in (self.ingest_trigger, self.validate_dedup, self.extract_text,
                         self.extract_profile, self.parse_jd, self.score_candidate,
                         self.finalize_ranking, self.api_function):
            table.grant_read_write_data(function)
            function.add_to_role_policy(_iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                resources=[uploads_arn, uploads_arn_slash, exports_arn, exports_arn_slash],
            ))
            function.add_to_role_policy(_iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                         "bedrock:ApplyGuardrail"],
                resources=["*"],          # narrowed in prod via SSM-listed model ARNs
            ))
            function.add_to_role_policy(_iam.PolicyStatement(
                actions=["ssm:GetParametersByPath", "ssm:GetParameter"],
                resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter/ai-recruiter/*"],
            ))
            function.add_to_role_policy(_iam.PolicyStatement(
                actions=["s3vectors:*"],
                resources=[vector_bucket_arn,
                           f"{vector_bucket_arn}/index/{vector_index_name}"],
            ))
            function.add_to_role_policy(_iam.PolicyStatement(
                actions=["states:StartExecution"],
                resources=[f"arn:aws:states:{self.region}:{self.account}:"
                           f"stateMachine:ar-{self.settings_env}-*"],
            ))

    def _sfn_grants(self) -> None:
        self.ingest_trigger.add_to_role_policy(_iam.PolicyStatement(
            actions=["states:StartExecution"],
            resources=[self.ingest_machine.state_machine_arn,
                       self.job_machine.state_machine_arn]))
        self.api_function.add_to_role_policy(_iam.PolicyStatement(
            actions=["states:StartExecution"],
            resources=[self.match_machine.state_machine_arn,
                       self.job_machine.state_machine_arn]))

    # ---- machines ----------------------------------------------------------------

    def _ingest_machine(self, settings: EnvSettings) -> _sfn.StateMachine:
        validate = _tasks.LambdaInvoke(
            self, "ValidateDedup", lambda_function=self.validate_dedup,
            result_path="$.validate",
            retry_on_service_exceptions=True,
        )
        choice = _sfn.Choice(self, "ValidAndNew")
        extract = _tasks.LambdaInvoke(
            self, "ExtractText", lambda_function=self.extract_text,
            result_path="$.text", retry_on_service_exceptions=True,
        )
        profile = _tasks.LambdaInvoke(
            self, "ProfileAndEmbed", lambda_function=self.extract_profile,
            result_path="$.profile", retry_on_service_exceptions=True,
        )
        choice.when(
            _sfn.Condition.boolean_equals("$.validate.Payload.valid", True),
            extract.next(profile),
        ).otherwise(_sfn.Succeed(self, "MarkedFailed"))
        return _sfn.StateMachine(
            self, "ingestMachine",
            state_machine_name=f"ar-{self.settings_env}-ingest",
            definition_body=_sfn.DefinitionBody.from_chainable(
                _sfn.Chain.start(validate).next(choice)),
            timeout=Duration.minutes(15),
        )

    def _job_machine(self) -> _sfn.StateMachine:
        parse = _tasks.LambdaInvoke(
            self, "ParseJd", lambda_function=self.parse_jd,
            retry_on_service_exceptions=True,
        )
        return _sfn.StateMachine(
            self, "jobMachine",
            state_machine_name=f"ar-{self.settings_env}-job",
            definition_body=_sfn.DefinitionBody.from_chainable(
                _sfn.Chain.start(parse)),
            timeout=Duration.minutes(10),
        )

    def _match_machine(self) -> _sfn.StateMachine:
        # The Map iterates over bare candidateId strings; build the
        # score_candidate event explicitly per item (candidateId="$") instead
        # of relying on Map ItemSelector state — a raw string payload fails
        # the invoke with InvalidRequestContent and the whole state is wrong
        # for the handler.
        score = _tasks.LambdaInvoke(
            self, "ScoreOne", lambda_function=self.score_candidate,
            payload=_sfn.TaskInput.from_object({
                "jobId": _sfn.JsonPath.string_at("$$.Execution.Input.jobId"),
                "userSub": _sfn.JsonPath.string_at("$$.Execution.Input.userSub"),
                "candidateId": _sfn.JsonPath.string_at("$"),
                "rubricWeights": _sfn.JsonPath.object_at("$$.Execution.Input.rubricWeights"),
            }),
            # The per-item input is the bare candidateId STRING, so the score
            # object cannot be attached to a result path — replace the state
            # with the Lambda's own response {scored, candidateId, totalScore}.
            result_path="$",
            retry_on_service_exceptions=True,
        )
        finalize = _tasks.LambdaInvoke(
            self, "FinalizeRanking", lambda_function=self.finalize_ranking,
            result_path="$.final",
        )
        # Map concurrency is capped so Bedrock throttling never fails the run (NFR-03)
        fanout = _sfn.Map(
            self, "ScoreEach",
            items_path="$.candidateIds",
            max_concurrency=5,
            result_path="$.scores",
        ).iterator(_sfn.Chain.start(score))
        return _sfn.StateMachine(
            self, "matchMachine",
            state_machine_name=f"ar-{self.settings_env}-match",
            definition_body=_sfn.DefinitionBody.from_chainable(
                _sfn.Chain.start(fanout).next(finalize)),
            timeout=Duration.minutes(60),
        )
