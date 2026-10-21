"""Live verification for the job-match restructure (Phases 0-3).

Invokes the API Lambda directly (same pattern as test_api.py) with fabricated
JWT claims (the handler trusts the edge-authorizer path), then polls:
  1. POST /jobs with pasted JD text -> PARSING (202)
  2. GET /jobs/{id} until READY -> title must be extracted (not untitled),
     jdText must be persisted (Phase 0 + Phase 1)
  3. POST /jobs/{id}/match with a READY candidate -> poll rankings (Phase 3)
"""
import json
import sys
import time

import boto3

REGION = "ap-south-1"
FUNCTION_NAME = "ai-recruiter-dev-backend-apiC8550315-Hkm6ldLKdlAu"
SUB = "1143dd8a-c041-70b6-dad1-d03abc481b27"

JD = """Senior Python Engineer — Payments Team

We are looking for a Senior Python Engineer to join our payments platform team.

Required skills: Python, FastAPI, PostgreSQL, AWS, Docker
Preferred skills: Kotlin, Terraform, Kafka
Minimum 5 years of professional backend experience. Senior level.
Responsibilities:
- Design and operate payment services on AWS
- Build clean, tested Python APIs
- Mentor mid-level engineers
"""


def make_event(method, path, path_params=None, body=None):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "test@example.com"}}},
        },
        "pathParameters": path_params or {},
        "queryStringParameters": {},
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body) if body else "",
    }


def call(lambda_client, method, path, path_params=None, body=None):
    resp = lambda_client.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(make_event(method, path, path_params, body)),
    )
    payload = json.loads(resp["Payload"].read())
    status = payload.get("statusCode")
    body_json = json.loads(payload.get("body") or "{}")
    return status, body_json


def main():
    lam = boto3.client("lambda", region_name=REGION)
    ok = True

    # 1. Create job from pasted JD (Phase 1: jdText persisted)
    status, body = call(lam, "POST", "/jobs", body={"jdText": JD})
    print("POST /jobs ->", status, str(body)[:120])
    if status != 202 or "jobId" not in body:
        print("FAIL: job creation failed")
        return 1
    jid = body["jobId"]

    # 2. Poll until parsed
    job = None
    for i in range(30):
        time.sleep(5)
        status, job = call(lam, "GET", f"/jobs/{jid}")
        if status == 200 and job.get("status") not in ("PARSING",):
            break
    print("GET /jobs -> status", job.get("status"))
    title = job.get("title", "")
    jd_text = job.get("jdText", "")
    reqs = job.get("parsedRequirements") or {}
    print("title:", title)
    print("jdText persisted:", bool(jd_text), f"({len(jd_text)} chars)")
    print("requiredSkills:", reqs.get("requiredSkills"))
    if "(untitled job)" in title:
        print("FAIL: title not extracted (Phase 0)")
        ok = False
    if not jd_text:
        print("FAIL: jdText not persisted (Phase 1)")
        ok = False

    # 3. Match a READY candidate (Phase 3 path backend-side)
    _, cl = call(lam, "GET", "/candidates")
    ready = [c for c in cl.get("candidates", []) if c.get("status") == "READY"]
    print("READY candidates:", len(ready))
    if ready:
        cid = ready[0]["candidateId"]
        status, body = call(lam, "POST", f"/jobs/{jid}/match", None, {"candidateIds": [cid]})
        print("POST /match ->", status, str(body)[:120])
        for i in range(20):
            time.sleep(5)
            _, rl = call(lam, "GET", f"/jobs/{jid}/rankings")
            rows = [r for r in rl.get("rankings", []) if r.get("candidateId") == cid]
            if rows:
                r = rows[0]
                print("ranking:", r.get("totalScore"), r.get("band"), "| criteria:", len((r.get("criteria") or r.get("analysis", {}).get("criteria") or [])))
                break
        else:
            print("WARN: no ranking row yet (may need more time)")
            ok = False
    else:
        print("WARN: no READY candidate to match; skipping 202 match check")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
