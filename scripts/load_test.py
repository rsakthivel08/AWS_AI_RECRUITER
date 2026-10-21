#!/usr/bin/env python3
"""Load test (doc section 8): N concurrent presigned uploads + M-candidate match.

Run against a deployed dev stack with a token:
  python scripts/load_test.py --api-url https://... --token <jwt> --files 50
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import json
import time
import urllib.request


def presign(base: str, token: str, filename: str) -> dict:
    request = urllib.request.Request(f"{base}/uploads/presign", method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    body = json.dumps({"kind": "resume", "filename": filename,
                       "sizeBytes": 5000}).encode()
    with urllib.request.urlopen(request, data=body, timeout=30) as resp:
        return json.loads(resp.read())


def upload(url: str, content: bytes) -> int:
    request = urllib.request.Request(url, method="PUT", data=content)
    request.add_header("Content-Type", "application/pdf")
    with urllib.request.urlopen(request, timeout=60) as resp:
        return resp.status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--files", type=int, default=50)
    args = parser.parse_args()
    base = args.api_url.rstrip("/")

    started = time.time()
    with futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(
            lambda i: (presign(base, args.token, f"load-{i}.pdf"),
                       upload(*presign(base, args.token, f"load-{i}.pdf")[0:1])),
            range(args.files)))
    ok = sum(1 for pair in results if pair[0].get("resourceId"))
    print(f"uploads started: {ok}/{args.files} in {time.time()-started:.1f}s")
    print("candidates should reach READY within 60s for 2-page resumes (NFR-01);"
          " poll GET /candidates to verify. This script does not wait.")
    return 0 if ok == args.files else 1


if __name__ == "__main__":
    raise SystemExit(main())
