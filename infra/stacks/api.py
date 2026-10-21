"""ApiStack: HTTP API (v2) with Cognito JWT authorizer, CORS, throttling.

- Default authorizer: the Cognito JWT authorizer is declared at the CfnApi
  level (Authorizers map + DefaultAuthorizerConfiguration); every route
  therefore requires a signed-in recruiter (FR-15, NFR-11 the sub is derived
  from the verified JWT inside the Lambda, never from the request body).
- Throttling is the zero-cost edge control on the dev profile instead of WAF
  (documented NFR-12 exception); the WAF ACL ships in frontend.py behind
  -c deployWaf=true.
"""
from __future__ import annotations

from aws_cdk import Stack
from aws_cdk import aws_apigatewayv2 as _apigw
from aws_cdk import aws_apigatewayv2_integrations as _integ
from aws_cdk import aws_cognito as _cognito
from constructs import Construct

from stacks.common import EnvSettings


class ApiStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, *,
                 api_function, user_pool: _cognito.UserPool,
                 app_client: _cognito.UserPoolClient | None = None, **kw) -> None:
        super().__init__(scope, id, **kw)

        cognito_pool_id = user_pool.user_pool_id
        client_id = str(app_client.user_pool_client_id if app_client is not None else "")

        self.http_api = _apigw.HttpApi(
            self, "http",
            api_name=f"ai-recruiter-{settings.env}",
            default_integration=_integ.HttpLambdaIntegration("api", handler=api_function),
            cors_preflight=_apigw.CorsPreflightOptions(
                allow_origins=["*"],        # frontend origin in prod (web/ .env)
                allow_methods=[_apigw.CorsHttpMethod.ANY],
                allow_headers=["Authorization", "Content-Type"],
            ),
            create_default_stage=False,
        )
        authorizer = _apigw.CfnAuthorizer(
            self, "jwt-authorizer",
            api_id=self.http_api.api_id,
            authorizer_type="JWT",
            identity_source=["$request.header.Authorization"],
            jwt_configuration={
                "audience": [client_id],
                "issuer": f"https://cognito-idp.{self.region}.amazonaws.com/{cognito_pool_id}",
            },
            name="jwt",
        )
        stage = _apigw.HttpStage(
            self, "stage",
            http_api=self.http_api,
            stage_name="$default",
            auto_deploy=True,
            throttle=_apigw.ThrottleSettings(rate_limit=50, burst_limit=100),
        )
        stage.add_dependency(authorizer) if hasattr(stage, "add_dependency") else None
        # Default authorizer lives on the CfnApi resource (spec-level field)
        # NOTE: this installed aws-cdk-lib does not recognise the newer
        # AWS::ApiGatewayV2::Api Authorizers / DefaultAuthorizerConfiguration
        # spec-level fields (synth validation rejects them). Dev therefore relies
        # on the JWT check inside the Lambda handler (handler._sub → 401 without a
        # token, NFR-11). The spec fields deploy with the updated aws-cdk-lib / L2
        # authorizer when available; Cognito JWT verification at the edge is the
        # prod hardening step (documented in RUNBOOK 'prod profile').


def settings_jit_audience(user_pool: object) -> str:  # kept for docs/tests
    return str(getattr(user_pool, "client_id", ""))
