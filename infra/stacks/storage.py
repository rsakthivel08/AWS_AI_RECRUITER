"""StorageStack: uploads / exports buckets + S3 Vectors bucket + index + optional KMS.

Dev profile (Free plan): SSE-S3 (AES-256) everywhere — no customer-managed key
charge. The CMK is created only with  -c deployCmk=true  (documented NFR-08
exception). Glass doors closed: block-public-access + enforce TLS (NFR-08).

S3 Vectors (GA, ap-south-1 per plan): bucket + index with Titan V2 geometry
(1024 dims, cosine).
"""
from __future__ import annotations

from aws_cdk import CfnResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_kms as _kms
from aws_cdk import aws_s3 as _s3
from constructs import Construct

from stacks.common import EnvSettings


class StorageStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, **kw) -> None:
        super().__init__(scope, id, **kw)
        prefix = f"ar-{settings.env}"

        self.cmk = (
            _kms.Key(self, "cmk", enable_key_rotation=True) if settings.deploy_cmk else None
        )

        def bucket(identifier: str, *, versioned: bool = False,
                   retention_only_uploads: bool = False) -> _s3.Bucket:
            common_kwargs = dict(
                bucket_name=f"{prefix}-{identifier.lower()}",
                block_public_access=_s3.BlockPublicAccess.BLOCK_ALL,
                enforce_ssl=True,
                versioned=versioned,
                encryption=_s3.BucketEncryption.KMS
                if (self.cmk is not None and identifier == "uploads")
                else _s3.BucketEncryption.S3_MANAGED,
                encryption_key=self.cmk if self.cmk is not None and identifier == "uploads" else None,
                object_ownership=_s3.ObjectOwnership.OBJECT_WRITER,
                removal_policy=(
                    RemovalPolicy.DESTROY if settings.env != "prod" else RemovalPolicy.RETAIN
                ),
            )
            if retention_only_uploads:
                common_kwargs["lifecycle_rules"] = [
                    _s3.LifecycleRule(
                        id="dpd-retention",
                        expiration=Duration.days(180),       # NFR-21 default retention
                        noncurrent_version_expiration=Duration.days(30),
                    )
                ]
            if identifier == "uploads":
                # Browser SPA PUTs files straight to presigned URLs; without a
                # CORS rule Chrome fails with "TypeError: Failed to fetch".
                common_kwargs["cors"] = [
                    _s3.CorsRule(
                        allowed_methods=[_s3.HttpMethods.PUT, _s3.HttpMethods.GET, _s3.HttpMethods.HEAD],
                        allowed_origins=["*"],   # API issues presigned URLs only to authenticated users
                        allowed_headers=["*"],
                        max_age=Duration.hours(1),
                    )
                ]
            return _s3.Bucket(self, identifier, **common_kwargs)

        self.uploads_bucket = bucket("uploads", versioned=True, retention_only_uploads=True)
        self.text_bucket = self.uploads_bucket           # text/ + vectors/ live here as prefixes
        self.exports_bucket = bucket("exports")

        # ---- S3 Vectors bucket + index (L1 resources; L2 constructs not yet in CDK)
        self.vector_bucket_name = f"{prefix}-vectors"
        self.vector_index_name = "resume-vectors"
        self._vector_bucket = CfnResource(
            self, "vectorBucket",
            type="AWS::S3::VectorBucket",
            properties={"vectorBucketName": self.vector_bucket_name,
                        "encryptionConfiguration": {"sseType": "SSE"}}
            if not self.cmk else {
                "vectorBucketName": self.vector_bucket_name,
            },
        )
        self._vector_index = CfnResource(
            self, "vectorIndex",
            type="AWS::S3::VectorIndex",
            properties={
                "vectorBucketName": self.vector_bucket_name,
                "indexName": self.vector_index_name,
                "dataType": "float32",
                "dimension": 1024,           # Titan Text Embeddings V2 default dims
                "distanceMetric": "cosine",
            },
        )
        self._vector_index.add_depends_on(self._vector_bucket)
        self.vector_bucket_arn = (
            f"arn:aws:s3vectors:"
            f"{self.region}:{self.account}:vector-bucket/{self.vector_bucket_name}"
        )
