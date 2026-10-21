"""AuthStack: Cognito user pool with hosted-UI app client (PKCE)."""
from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_cognito as _cognito
from constructs import Construct

from stacks.common import EnvSettings


class AuthStack(Stack):
    def __init__(self, scope: Construct, id: str, settings: EnvSettings, **kw) -> None:
        super().__init__(scope, id, **kw)
        self.user_pool = _cognito.UserPool(
            self, "recruiters",
            user_pool_name=f"recruiters-{settings.env}",
            self_sign_up_enabled=True,
            sign_in_case_sensitive=False,
            sign_in_aliases=_cognito.SignInAliases(email=True),
            account_recovery=_cognito.AccountRecovery.EMAIL_ONLY,
            password_policy=_cognito.PasswordPolicy(
                min_length=8,
                require_symbols=False,
                require_digits=True,
                require_uppercase=False,
            ),
            standard_attributes={
                "email": _cognito.StandardAttribute(required=True, mutable=True),
            },
            removal_policy=(None if settings.env == "prod"
                            else self._destroy_policy()),
        )
        self.client = self.user_pool.add_client(
            "recruiterAppClient",
            auth_flows=_cognito.AuthFlow(
                user_srp=True, user_password=True),
            generate_secret=False,              # public client (PKCE via hosted UI)
            o_auth=_cognito.OAuthSettings(
                flows=_cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[_cognito.OAuthScope.EMAIL, _cognito.OAuthScope.OPENID,
                        _cognito.OAuthScope.PROFILE],
                callback_urls=["http://localhost:5173/auth/callback"],
                logout_urls=["http://localhost:5173/"],
            ),
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
        )
        self.domain = self.user_pool.add_domain(
            "hostedUiDomain",
            cognito_domain=_cognito.CognitoDomainOptions(domain_prefix=f"ar-{settings.env}-recruiter"),
        )

    def _destroy_policy(self):
        from aws_cdk import RemovalPolicy

        return RemovalPolicy.DESTROY
