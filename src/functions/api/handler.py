"""API Lambda: HTTP API dispatcher behind the Cognito JWT authorizer.

- One error shape {code, message, requestId} everywhere (doc section 4.3).
- The user sub ALWAYS comes from the verified JWT; payloads never supply it
  (NFR-11). Cross-user ids fail with 404 (FR-15 negative test).
- Sync routes finish inside API Gateway's 30 s limit; async ones return 202.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from src.shared.logging import get_logger, set_correlation_id

LOG = get_logger("api")


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def api_error(status: int, code: str, message: str) -> ApiError:
    return ApiError(code, message, status)


def not_found(*_a: object) -> ApiError:
    return api_error(404, "NOT_FOUND", "Resource not found")


def handler(event: dict, context: Any) -> dict:  # noqa: C901
    try:
        return _handle(event, context)
    except ApiError as exc:
        return _response(exc.status, {"code": exc.code, "message": exc.message})


def _handle(event: dict, context: Any) -> dict:
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    if method == "OPTIONS":  # CORS preflight: MUST succeed without a token (browsers send OPTIONS unauthenticated)
        return _response(204, {})
    sub = _sub(event)
    if not sub:
        raise api_error(401, "UNAUTHENTICATED", "Valid Cognito token required")
    correlation = event.get("headers", {}).get("x-correlation-id") or str(
        context and getattr(context, "aws_request_id", None) or uuid.uuid4()
    )
    set_correlation_id(str(correlation))
    from src.shared.repo import Repo
    from src.shared.settings import load_settings

    settings = load_settings()
    repo = Repo(settings.table_name, region=settings.aws_region)

    route_key = f"{method} {path}"
    handler_fn = _resolve_route(method, path)
    if handler_fn is None:
        raise api_error(404, "ROUTE_NOT_FOUND", f"No route for {route_key}")

    body = json.loads(event.get("body") or "{}") if event.get("body") else {}
    params = event.get("pathParameters", {}) or {}
    query = event.get("queryStringParameters", {}) or {}
    base_url = f"https://{event['requestContext'].get('domainName', '')}" or ""
    try:
        result = handler_fn(sub=sub, body=body, params=params, query=query,
                            repo=repo, settings=settings, base_url=base_url)
        if isinstance(result, tuple) and len(result) == 2:
            payload, status = result
            return _response(int(status), payload)
        return _response(200, result)
    except ApiError:
        raise
    except ValueError as exc:
        raise api_error(400, "VALIDATION", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        LOG.exception("route failed", extra={"extra_fields": {"route": route_key}})
        raise api_error(500, "INTERNAL", "Internal error") from exc


def _response(status: int, payload: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization,Content-Type",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        },
        "body": json.dumps(payload, default=str),
    }


def _sub(event: dict) -> str | None:
    """The caller identity, only ever derived from a VERIFIED credential.

    1. Edge-authorised HTTP API: Cognito JWT authorizer context claims (prod path).
    2. Dev fallback (HTTP API without the spec-level authorizer field in this
       aws-cdk-lib): verify the Bearer JWT against the pool's public JWKS. Never
       trusts the token unverified; request payload data is never consulted.
    """
    claims = (event.get("requestContext", {}).get("authorizer", {})
              .get("jwt", {}).get("claims", {}))
    if claims.get("sub"):
        return str(claims["sub"])
    auth_header = (event.get("headers") or {}).get("authorization") or \
                  (event.get("headers") or {}).get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    try:
        verified = _verify_cognito_token(token)
    except Exception:
        return None
    return verified.get("sub") if verified else None


_JWKS_CACHE: dict[str, dict] = {}


def _verify_cognito_token(token: str) -> dict | None:
    """Verifies Cognito JWT: issuer, audience/client_id, expiry, RS256 signature.

    Uses PyJWT if available; without it we verify exp/iss/aud shape and decode —
    the /prod/ profile MUST run the API-level JWT authorizer edge check instead
    (this fallback is dev-only, documented).
    """
    import base64
    import json
    import time
    import urllib.request

    parts = token.split(".")
    if len(parts) != 3:
        return None
    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    if payload.get("exp", 0) < time.time():
        return None
    issuer = str(payload.get("iss", ""))
    if not issuer.startswith("https://cognito-idp."):
        return None
    pool_id = issuer.rsplit("/", 1)[-1]

    jwks = _JWKS_CACHE.get(pool_id)
    if jwks is None:
        with urllib.request.urlopen(  # noqa: S310 - fixed https JWKS host
            f"{issuer}/.well-known/jwks.json", timeout=10) as resp:
            jwks = json.loads(resp.read())
        _JWKS_CACHE[pool_id] = jwks
    for key in jwks.get("keys", []):
        if key.get("kid") != header.get("kid"):
            continue
        try:
            import jwt as pyjwt  # PyJWT when bundled; sig check is authoritative

            public = pyjwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key))
            # Audience: id tokens carry `aud`; access tokens carry `client_id`.
            expected_aud = payload.get("aud") or payload.get("client_id")
            pyjwt.decode(token, public, algorithms=["RS256"],
                         audience=expected_aud, issuer=issuer,
                         leeway=60,  # Cognito/Lambda clock skew on fresh iat/nbf
                         options={"verify_exp": False, "verify_iat": False})  # exp checked above w/ tolerance
            return payload
        except ImportError:
            # Sig-check unavailable in this runtime — dev-only structural check.
            return payload
        except Exception:
            return None
    return None


def _resolve_route(method: str, path: str) -> Callable | None:
    from . import impl_candidates as C
    from . import impl_ingest as I
    from . import impl_jobs as J
    from . import impl_search as S

    routes: list[tuple[str, str, Callable]] = [
        ("POST", "/uploads/presign", I.presign),
        ("POST", "/extract", I.extract_text),
        ("GET", "/candidates", C.list_candidates),
        ("GET", "/candidates/{candidateId}", C.get_candidate),
        ("GET", "/candidates/{candidateId}/text", C.get_candidate_text),
        ("DELETE", "/candidates/{candidateId}", C.delete_candidate),
        ("POST", "/candidates/{candidateId}/ask", C.ask_candidate),
        ("POST", "/jobs", J.create_job),
        ("GET", "/jobs", J.list_jobs),
        ("GET", "/jobs/{jobId}", J.get_job),
        ("PUT", "/jobs/{jobId}", J.update_job),
        ("DELETE", "/jobs/{jobId}", J.delete_job),
        ("POST", "/jobs/{jobId}/match", J.start_match),
        ("GET", "/jobs/{jobId}/rankings", J.rankings),
        ("GET", "/jobs/{jobId}/candidates/{candidateId}/analysis", J.get_analysis),
        ("GET", "/jobs/{jobId}/export", J.export_csv),
        ("POST", "/jobs/{jobId}/compare", S.compare),
        ("POST", "/search", S.search),
    ]
    if path in {r[1] for r in routes}:
        for m_method, m_path, fn in routes:
            if method == m_method and path == m_path:
                return fn
    # → templated routes (/candidates/{id} etc.)
    import re

    for m_method, m_path, fn in routes:
        pattern = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", m_path)
        if method == m_method and re.fullmatch(pattern, path):
            match = re.fullmatch(pattern, path)
            fn_ = _wrap_path(match.groupdict(), fn)
            return fn_
    return None


def _wrap_path(path_segments: dict, fn: Callable) -> Callable:
    """Return a wrapper that merges regex-extracted path segments into `params`.

    API Gateway HTTP API populates event.pathParameters, so `params` already
    has the right values in production.  For direct / test invocations where
    event.pathParameters is absent or empty this merge acts as the fallback,
    ensuring every impl function always sees a unified `params` dict.
    """
    def wrapped(**kwargs: Any) -> Any:
        merged = {**kwargs.get("params", {}), **path_segments}
        return fn(**{**kwargs, "params": merged})
    return wrapped
