#!/usr/bin/env python3
"""Post-deploy smoke test (doc section 7.2).

Usage: python scripts/smoke_test.py --api-url https://... --token <jwt>
Checks: unauthenticated 401, authenticated /jobs 200, presign 400 on bad type.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def call(url: str, token: str | None, data: dict | None = None, method: str = "GET") -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    body = json.dumps(data).encode() if data else None
    try:
        with urllib.request.urlopen(request, data=body, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token", default=None)
    args = parser.parse_args()
    base = args.api_url.rstrip("/")
    failures: list[str] = []

    status, _ = call(f"{base}/jobs", None)
    if status != 401:
        failures.append(f"unauthenticated /jobs returned {status}, expected 401")

    status, payload = call(f"{base}/jobs", args.token)
    if status != 200:
        failures.append(f"authenticated /jobs returned {status}, expected 200")

    status, payload = call(f"{base}/uploads/presign", args.token,
                           {"kind": "resume", "filename": "x.exe", "sizeBytes": 10},
                           method="POST")
    if status != 400:
        failures.append(f"bad-type presign returned {status}, expected 400")

    status, _ = call(f"{base}/candidates/c_doesnotexist", args.token)
    if status != 404:
        failures.append(f"missing candidate returned {status}, expected 404")

    if failures:
        print("SMOKE FAIL:\n  " + "\n  ".join(failures))
        return 1
    print("SMOKE OK: 401/200/400/404 all as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
