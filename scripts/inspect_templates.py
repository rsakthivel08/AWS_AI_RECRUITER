#!/usr/bin/env python3
"""Verify cdk.out templates contain the expected resources (local synth check)."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent / "infra" / "cdk.out"
if not root.exists():
    print("cdk.out missing — run cdk synth first")
    sys.exit(1)

EXPECTED = {
    "ai-recruiter-dev-backend.template.json": [
        "AWS::S3::Bucket",
        "AWS::DynamoDB::Table",
        "AWS::Lambda::Function",
        "AWS::StepFunctions::StateMachine",
        "AWS::SQS::Queue",
        "AWS::S3::VectorBucket",
        "AWS::S3::VectorIndex",
    ],
    "ai-recruiter-dev-auth.template.json": ["AWS::Cognito::UserPool",
                                            "AWS::Cognito::UserPoolClient"],
    "ai-recruiter-dev-api.template.json": ["AWS::ApiGatewayV2::Api",
                                           "AWS::ApiGatewayV2::Authorizer",
                                           "AWS::ApiGatewayV2::Stage"],
    "ai-recruiter-dev-frontend.template.json": ["AWS::CloudFront::Distribution",
                                                "AWS::S3::Bucket"],
    "ai-recruiter-dev-observability.template.json": ["AWS::Budgets::Budget"],
}

failures = []
for filename, needed in EXPECTED.items():
    path = root / filename
    if not path.exists():
        failures.append(f"{filename}: missing")
        continue
    template = json.loads(path.read_text())
    types = {res.get("Type") for res in template.get("Resources", {}).values()}
    for resource_type in needed:
        if resource_type not in types:
            failures.append(f"{filename}: missing {resource_type}")
    if filename.endswith("api.template.json"):
        blob = json.dumps(template)
        for needle in ("DefaultAuthorizerConfiguration", "Authorizers", "JWT"):
            if needle not in blob:
                failures.append(f"api: missing authorizer field {needle}")

if failures:
    print("SYNTH CHECK FAIL:\n  " + "\n  ".join(failures))
    sys.exit(1)
print("SYNTH CHECK OK: all expected resources present in every template")
