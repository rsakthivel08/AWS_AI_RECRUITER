# Runbook

## One-time account setup (Free plan, doc §7.4)

1. **Credits:** Billing → Free credits. Screenshot the balance + expiry from
   "Credit preferences"; activity credits expire per their individual dates.
2. **Model access:** NO console step exists anymore — the Model access page was
   retired and Amazon serverless models (Nova, Titan) auto-enable on first
   invocation. If invocations fail with `ValidationException: Operation not
   allowed` in every region/model on a new account, it is the account-level
   Bedrock verification hold; file one Support case (it also covers the
   CloudFront verification hold) — see HANDOFF.md for the exact ticket text.
   (Anthropic models additionally need the one-time first-use form — only
   needed if you later swap the judge model via SSM.)
3. **Quotas:** Business-model TPM/RPM here are NOT adjustable on a fresh
   Free-plan account (API rejects with "not adjustable") — the match Map caps
   at 5 concurrent, which needs roughly 20k TPM for Nova Micro; if quota
   requests/verification clear the account, confirm via
   `aws service-quotas get-service-quota --region ap-south-1 --service-code
   bedrock --quota-code L-DC7FF66C --query Quota.Value` (want ≥ 20000).
   See HANDOFF.md for the filed requests (L-DC7FF66C, PENDING).
4. **Bootstrap** (once per account+region):
   `cd infra && npx aws-cdk bootstrap aws://<account>/ap-south-1`

## Deploy / teardown

```
cd infra
npx aws-cdk deploy -c env=dev --all          # dev profile, ~zero idle cost
npx aws-cdk destroy -c env=dev --all         # reclaim everything
```

Prod additionally takes `-c env=prod -c alarmEmail=<you@example.com>` and
enables PITR/90-day logs. `deployWaf=true` / `deployCmk=true` render the
documented prod extras.

## Common incidents

| Symptom | Check | Fix |
|---|---|---|
| Candidate stuck PROCESSING >2 min | Step Functions console, ingest executions | Inspect the failed step; `FailureReason` on the candidate item states the cause (unsupported type, oversized, no text layer) |
| DLQ depth alarm fired | SQS validate/score DLQ | Re-drive messages after fixing root cause; items are idempotent |
| `BedrockThrottledException` bursts | Bedrock CloudWatch metrics | Lower Map `max_concurrency` to 3 in `pipeline.py`; the retry already backs off |
| 429 from /jobs/{id}/match | Candidate `USAGE#<date>` item | Daily limit is configurable (`DAILY_MATCH_LIMIT` env); wait or raise deliberately |
| Presign returns 400 | Filetype extension or sizeBytes > 10 MB | Both validated server-side pre-upload (NFR-14) |
| Prompt-version regression | `/prompts/<name>/vN.md` + golden set | Re-run golden set (doc §8), keep the old vN.md — it is retained by design |
| Vector query returns nothing | `VECTOR_BUCKET`/`VECTOR_INDEX` env, S3 Vectors index | Check `vectors/<sub>/<cid>.json` fallback path — the code silently falls back and logs to its Lambda's CloudWatch |

## Credit spend guard (plan §6)

- $0-spend AWS Budget auto-created by the Observability stack at 50/80/100%.
- Bedrock is the only large-cost service; the daily usage counter caps matches.
- Deleting dev data between demo runs reclaims almost nothing — spend is per
  invocation, not per stored object. **Destroy the stack** to stop billing.

## Demo script (doc §13, ordered)

1. Sign in (Cognito hosted UI) → dashboard counts load.
2. Upload 10 resumes via presigned URLs → watch PROCESSING → READY.
3. Create a job from pasted JD text → parsed requirements appear → edit one.
4. POST match → ranking table with bands.
5. Open an analysis → criterion bars with evidence quotes, gap table,
   claim checks, flags, screening questions.
6. Compare 3 candidates → matrix + trade-off summary.
7. Search query in natural language → matched skills + "why" per result.
8. Ask the candidate a free-form question → grounded answer with citations.
9. Delete a candidate → confirm S3, DynamoDB and vectors are all gone.
