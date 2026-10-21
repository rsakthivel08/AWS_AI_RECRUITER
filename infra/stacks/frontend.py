"""FrontendStack: SPA hosting.

Default (Free-plan dev): S3 static website hosting — zero-cost and no
pre-verification. The React app calls the HTTP API directly (CORS allowed).
CloudFront (doc A2 / prod target) deploys with -c hostMode=cloudfront once the
account is verified for CloudFront resources (AWS Support flow; documented
Free-plan exception). -c hostMode=s3 is the dev default.
"""
from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as _cloudfront
from aws_cdk import aws_cloudfront_origins as _origins
from aws_cdk import aws_iam as _iam
from aws_cdk import aws_s3 as _s3
from aws_cdk import aws_wafv2 as _waf
from constructs import Construct

from stacks.common import EnvSettings


class FrontendStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings,
                 deploy_waf: bool = False, **kw) -> None:
        super().__init__(scope, id, **kw)
        self.host_mode = self.node.try_get_context("hostMode") or "s3"

        self.web_bucket = _s3.Bucket(
            self, "web",
            bucket_name=f"ar-{settings.env}-web-{self.account}",
            block_public_access=_s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=not self._s3_hosting() or False,
            versioned=True,
            encryption=_s3.BucketEncryption.S3_MANAGED,
            website_index_document="index.html",
            website_error_document="index.html",
            removal_policy=(RemovalPolicy.DESTROY if settings.env != "prod"
                            else RemovalPolicy.RETAIN),
        )
        if not self._s3_hosting():
            self.distribution = _cloudfront.Distribution(
                self, "cdn",
                default_behavior=_cloudfront.BehaviorOptions(
                    origin=_origins.S3BucketOrigin(self.web_bucket),
                    viewer_protocol_policy=_cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=_cloudfront.CachePolicy.CACHING_OPTIMIZED,
                ),
                default_root_object="index.html",
                error_responses=[],
                price_class=_cloudfront.PriceClass.PRICE_CLASS_100,
            )
            if deploy_waf:
                self._waf_acl()
            return

        # ---- S3 static website hosting (dev default) ---------------------------
        # Public read policy, scoped to bucket principals is not possible with
        # BLOCK_ALL; use `public_read_access` via a scoped policy statement.
        self.web_bucket.add_to_resource_policy(
            _iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{self.web_bucket.bucket_arn}/*"],
                principals=[_iam.AnyPrincipal()],
            ),
        )
        # S3 website hosting needs the public-access block relaxed for this bucket
        self.web_bucket.node.default_child.add_property_override("PublicAccessBlockConfiguration", {
            "BlockPublicAcls": False,
            "BlockPublicPolicy": False,
            "IgnorePublicAcls": False,
            "RestrictPublicBuckets": False,
        })
        self.web_url = (
            f"http://{self.web_bucket.bucket_name}.s3-website.{self.region}.on.aws/"
            if False else
            f"http://{self.web_bucket.bucket_name}.s3-website-{self.region}.amazonaws.com/"
        )
        if deploy_waf:
            self._waf_acl()

    def _s3_hosting(self) -> bool:
        return self.host_mode == "s3"

    def _waf_acl(self) -> None:  # prod-only, packaged and gated
        _waf.CfnWebACL(
            self, "waf",
            scope="CLOUDFRONT",
            default_action={"allow": {}},
            visibility_config={"cloud_watch_metrics_enabled": True,
                               "metric_name": "arWaf", "sampled_requests_enabled": False},
            rules=[
                _waf.CfnWebACL.RuleProperty(
                    name="RateLimit",
                    priority=1,
                    statement=_waf.CfnWebACL.StatementProperty(
                        rate_based_statement=_waf.CfnWebACL.RateBasedStatementProperty(
                            limit=2000, aggregate_key_type="IP")),
                    action={"block": {}},
                    visibility_config={"cloud_watch_metrics_enabled": True,
                                       "metric_name": "arRate", "sampled_requests_enabled": False},
                ),
                _waf.CfnWebACL.RuleProperty(
                    name="CommonRuleSet",
                    priority=2,
                    override_action={"none": {}},
                    statement=_waf.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=_waf.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesCommonRuleSet")),
                    visibility_config={"cloud_watch_metrics_enabled": True,
                                       "metric_name": "arCommon", "sampled_requests_enabled": False},
                ),
            ],
        )

    _ = Duration
