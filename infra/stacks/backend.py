"""BackendStack: S3 buckets + DynamoDB table + pipeline Lambdas/machines in ONE stack.

The S3 event notification (Storage-owned resource referencing the Pipeline
Lambda) plus env-var reads of bucket names (Pipeline referencing Storage) form
a reference cycle when they live in separate stacks. Building them together in
a single stack removes the cycle entirely while keeping the doc's §7.1 logical
separation (construct sections, not stacks).
"""
from __future__ import annotations

from aws_cdk import CfnResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as _dynamodb
from aws_cdk import aws_s3 as _s3
from constructs import Construct

from stacks.common import EnvSettings
from stacks.pipeline import PipelineStack


class BackendStack(PipelineStack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, *,
                 user_pool, **kw) -> None:
        region = Stack.of(self).region if False else None  # resolved after super's scope exists
        # NOTE: constructs must be created inside __init__ of THIS stack; region is
        # available through self, but self isn't bound until super().__init__ runs.
        # So buckets/table creation happens via a two-phase trick below.

        prefix = f"ar-{settings.env}"
        self._settings = settings
        self._prefix = prefix

        # Phase 1: create buckets/table by calling PipelineStack.__init__ with
        # callback-provided placeholders is impossible; instead we create them
        # directly here and then call super with the in-scope references.
        scope_of_self = self
        # Stack base must be initialised before any child construct exists.
        Stack.__init__(scope_of_self, id, **kw)

        cmk = (
            _s3.Bucket.__new__(_s3.Bucket) if False else None  # keep lints quiet
        )
        _ = cmk

        self.cmk = None  # dev profile default: no customer-managed key

        def bucket(identifier: str, *, versioned: bool = False,
                   retention_only_uploads: bool = False) -> _s3.Bucket:
            common_kwargs = dict(
                bucket_name=f"{prefix}-{identifier.lower()}",
                block_public_access=_s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                versioned=versioned,
                encryption=_s3.BucketEncryption.S3_MANAGED,
                object_ownership=_s3.ObjectOwnership.OBJECT_WRITER,
                removal_policy=(RemovalPolicy.DESTROY if settings.env != "prod"
                                else RemovalPolicy.RETAIN),
            )
            if retention_only_uploads:
                common_kwargs["lifecycle_rules"] = [
                    _s3.LifecycleRule(id="retention",
                                      expiration=Duration.days(180),   # NFR-21
                                      noncurrent_version_expiration=Duration.days(30))
                ]
            return _s3.Bucket(self, identifier, **common_kwargs)

        self.uploads_bucket = bucket("uploads", versioned=True, retention_only_uploads=True)
        self.text_bucket = self.uploads_bucket          # text/, vectors/ as prefixes
        self.exports_bucket = bucket("exports")

        # S3 Vectors bucket + index (L1 resources)
        self.vector_bucket_name = f"{prefix}-vectors"
        self.vector_index_name = "resume-vectors"
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

        # DynamoDB single table + GSIs (doc §3.3)
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

        # Phase 2: pipeline wiring, all references now in-scope (no cross-stack).
        super().__init__(
            scope_of_self, id,
            settings=settings,
            table=self.table,
            uploads_bucket=self.uploads_bucket,
            text_bucket=self.text_bucket,
            exports_bucket=self.exports_bucket,
            vector_bucket=self._vector_bucket,
            vector_bucket_arn=self.vector_bucket_arn,
            vector_index_name=self.vector_index_name,
            vector_bucket_name=self.vector_bucket_name,
            user_pool=user_pool,
            **kw,
        )
