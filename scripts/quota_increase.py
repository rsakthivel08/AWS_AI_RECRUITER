"""File Service-Quota increase requests for the zero-applied Bedrock quotas.

Run:  cd ai-recruiter-aws && .venv/Scripts/python.exe scripts/quota_increase.py

Background (see docs/HANDOFF.md): this new AWS account has applied=0 for the
Bedrock model quotas it needs, which makes every invocation fail with
"ValidationException: Operation not allowed". Quota-increase requests are a
plain Service Quotas API call and are the one self-service lever available on
the Free plan. File them once, then track:
  aws service-quotas list-requested-service-quota-change-history --service-code bedrock
"""

import json
import subprocess
import sys

# Quota -> descriptive name, desired applied value.
# NOTE (verified Oct 13, 2026 on this account):
# - Cross-region TPM quotas (L-DC7FF66C etc.) ARE adjustable, but the API
#   requires desired > default (8M in us-east-1) - so a productive request for
#   that quota code is 8,000,001+, not 20k. Filed automatically below.
# - Titan Embeddings V2 on-demand quotas (L-DE641971 / L-26C560CE) report
#   "not adjustable" on this account -> they need the Support ticket path.
REQUESTS = [
    ("L-DC7FF66C", 8000001, "us-east-1"),  # Nova Micro TPM: must exceed default 8,000,000
    ("L-DC7FF66C", 400001, "ap-south-1"),  # Nova Micro TPM: must exceed default 400,000
]


def run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, timeout=120).stdout


def pending_with_desired(code: str, region: str, desired: float) -> bool:
    """True if a PENDING/CASE_OPENED change for this exact request already exists."""
    out = run(
        "aws", "service-quotas", "list-requested-service-quota-change-history",
        "--region", region, "--service-code", "bedrock", "--output", "json",
    )
    try:
        entries = json.loads(out).get("RequestedQuotas", [])
    except json.JSONDecodeError:
        return False
    return any(
        e.get("QuotaCode") == code
        and e.get("DesiredValue") == desired
        and e.get("Status") in {"PENDING", "CASE_OPENED"}
        for e in entries
    )


def applied(service_code: str, code: str, region: str) -> float:
    out = run(
        "aws", "service-quotas", "get-service-quota",
        "--region", region, "--service-code", service_code,
        "--quota-code", code,
        "--query", "Quota.Value", "--output", "text",
    )
    return float(out.strip() or "nan")


def main() -> int:
    """Idempotent: re-runs skip non-zero quotas and dedup-pending ones.

    Exit codes: 0 = nothing needed filing (or all succeeded),
    1 = at least one filing failed (e.g. 'quota not adjustable' -> that one
    needs the Support/email routes in docs/HANDOFF.md instead).
    """
    rc = 0
    for code, desired, region in REQUESTS:
        current = applied("bedrock", code, region)
        print(f"[{region}] {code}: applied={current}, requesting={desired}")
        if current > 0:
            print("    already non-zero, skipping request")
            continue
        if pending_with_desired(code, region, desired):
            print("    an identical PENDING/CASE_OPENED request already exists, skipping")
            continue
        out = subprocess.run(
            [
                "aws", "service-quotas", "request-service-quota-increase",
                "--region", region, "--service-code", "bedrock",
                "--quota-code", code, "--desired-value", str(desired),
                "--query", "RequestedQuota.{Id:Id,Status:Status}",
                "--output", "text",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode == 0:
            print(f"    filed: {out.stdout.strip()}")
        else:
            print(f"    FAILED (rc={out.returncode}): {out.stderr.strip()[:300]}")
            rc = 1  # a non-adjustable quota here means the Support ticket is the path
    return rc


if __name__ == "__main__":
    sys.exit(main())
