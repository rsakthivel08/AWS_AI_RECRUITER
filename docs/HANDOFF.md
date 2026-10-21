# Handoff — AI-Recruiter AWS: what is deployed and the one step left

## Live URLs (verified working)

| Component | URL |
|---|---|
| API (HTTP API, JWT-protected) | `https://gvmmouvwtf.execute-api.ap-south-1.amazonaws.com` |
| Web SPA (S3 website endpoint) | `http://ar-dev-web-942893240068.s3-website-ap-south-1.amazonaws.com/` — after changing web code: `cd web && npm run build && aws s3 sync dist s3://ar-dev-web-942893240068 --delete` |
| Cognito user pool | `ap-south-1_cXvAsie7n` (hosted UI domain `ar-dev-recruiter`) |
| App client id | `786hee9ug9jen9njq5qfg538rq` |
| Test credentials | `test-recruiter@example.com` / `Recruiter#Dev1` (USER_PASSWORD_AUTH works; change for anything real) |
| Uploads bucket | `ar-dev-uploads` · Exports: `ar-dev-exports` · Web: `ar-dev-web-942893240068` |
| Step Functions | `ar-dev-ingest`, `ar-dev-job`, `ar-dev-match` |
| DynamoDB table | `recruiter-dev` (single table, gsi1 ranking, gsi2 dedup) |

## Verified live end-to-end (Oct 13, 2026)

1. **Unauthenticated calls → 401** with the `{code, message}` error shape.
2. **Cognito JWT works**: `initiate-auth` (USER_PASSWORD_AUTH) → `GET /jobs` 200 `{"jobs": []}`.
3. **Presign happy path** → 5-min regional PUT URL; **bad file type → 400** `UNSUPPORTED_FILE_TYPE`.
4. **Real PDF upload → HTTP 200** on the presigned URL.
5. **S3 ObjectCreated → ingest_trigger Lambda → Step Functions `ar-dev-ingest`** fired automatically.
6. **ValidateDedup AND ExtractText (PyMuPDF) succeeded inside Lambda** — text extraction works (the remaining failure is only the Bedrock call).

## The ONE remaining blocker: Bedrock account verification (one Support case)

The console's **Model access page has been retired** — your own screenshot of it
confirms Amazon serverless models (Nova, Titan embed) are auto-enabled on first
invocation, so there is literally nothing left to click there.

Reproduced root cause (Oct 13, 2026): every invocation fails with
`ValidationException: Operation not allowed` — in **us-east-1 AND ap-south-1**,
for **Nova Micro / Lite / Pro** and **Titan Text Embeddings V2**, for plain
model IDs and for `us.`/`apac.` inference profiles alike. AWS's Knowledge
Center classifies that exact message as an **account-level security
restriction** (an invisible verification hold on brand-new accounts that also
hit Bedrock/Textract/Rekognition for hackathon accounts in Sep 2026); it is not
fixable from IAM, the console, or the CLI — only AWS lifts it (via the
quota/verification/self-service routes below).

**Account state (verified Oct 13, 2026):** Free plan ACTIVE,
`freetier:GetAccountPlanState` → **$150.38 credits remaining, expires
2027-01-31** — so billing is not the blocker. The numeric evidence, plus what
exactly to paste into each path:

1. **Self-service (try first, no support-plan requirement):** the zeros are
   *explicitly-applied Bedrock quotas*, which are requestable via plain
   Service Quotas quota-increase requests:

```bash
# Applied TPM for us.amazon.nova-micro (cross-region profile) per region:
L-DC7FF66C  applied 0.0 in us-east-1, us-east-2, ap-south-1   (default 8,000,000!)
L-DE641971  applied 0.0 everywhere   (Titan Embeddings V2 on-demand TPM)
L-26C560CE  applied 0.0 everywhere   (Titan Embeddings V2 on-demand RPM)

cd ai-recruiter-aws && .venv/Scripts/python.exe scripts/quota_increase.py   # files them for you
```

   STATUS (verified at filing time, Oct 13, 2026): both adjustable requests
   were **filed** — `d26f501f5b93…TKfNBOYz` (L-DC7FF66C us-east-1, want
   8,000,001) and `bfa21490c358…mS7A3Jc` (L-DC7FF66C ap-south-1, want
   400,001) — and the first is already a **Support case, `CASE_OPENED`**
   (case id `179191320200456`). The Titan on-demand quotas came back
   **"not adjustable"** — those two need the email/ticket routes below.
   Track status with:

```bash
aws service-quotas list-requested-service-quota-change-history \
  --service-code bedrock --region us-east-1 --output json | grep -E '"Id|"Status'
```

   (Verified: the requests auto-created **Support case 179191320200456** —
   status `CASE_OPENED` for us-east-1; check the ap-south-1 one the same way.
   To check reopened/current state use `--query` on `RequestedQuotas[].Status`.)

2. **The bedrock-runtime error itself names the path** when the account gate
   is what throws (seen in us-east-2): *"Your account is currently being
   verified. Verification normally takes less than 2 hours. … If you are still
   receiving this message after more than 2 hours, please let us know by
   writing to aws-verification@amazon.com"* — so an email to
   **aws-verification@amazon.com** is the second self-service attempt (include
   the repro block, caller ARN, and a note that the account was created
   2026-10 with credits active and $150.38 remaining).
3. **Last resort (both above failed):** Support Center ticket as written
   below.


**File one Support case (it covers BOTH blockers — Bedrock and CloudFront):**

1. Go to **AWS Support Center → Create case → "Account and billing" or
   "Service limit increase/service issue"** (Basic plan tickets are free).
2. Attach these verified facts: account `942893240068`, caller
   `arn:aws:iam::942893240068:user/assignment-user`, account created Oct 2026,
   Free plan with credits active, and the exact repro:

```bash
aws bedrock-runtime converse --region us-east-1 --model-id "amazon.nova-micro-v1:0" \
  --messages '[{"role":"user","content":[{"text":"Say READY."}]}]'
# → An error occurred (ValidationException) when calling the Converse
#   operation: Operation not allowed
```

<details><summary>Ticket body (copy-paste)</summary>

> The same ValidationException is returned for every Bedrock model (Nova
> Micro/Lite/Pro, Titan Embeddings V2) in every region and every
> inference-profile ID. Service Quotas shows applied=0 for Bedrock codes
> L-DC7FF66C / L-DE641971 / L-26C560CE (us-east-1, us-east-2, ap-south-1) while
> documented defaults are 8,000,000 / 300,000 / 6,000. The account has credits
> ($150.38 remaining, Free plan ACTIVE) and a valid payment method on file.
> Please lift the account-level Bedrock security restriction, and also complete
> account verification for CloudFront (we get "Your account must be verified
> before you can add new CloudFront resources").

</details>

**Note — a Support case may already exist for you:** the quota-increase
requests above were accepted and auto-created **Support case
179191320200456** (`CASE_OPENED`); AWS correlates quota-change tickets with new
inquiries, so check that case's replies before opening a second one — reply in
the existing thread with the message from the `<details>` block instead.

3. Resolution is typically 1–2 business days (community reports range minutes
   to ~2 days on a Basic plan). When the converse call above succeeds, the
   account is unlocked for good — model access never needs touching again.
4. **Then, no code change is needed anywhere.** The pipeline is wired up to
   the Bedrock call; just upload a resume via the SPA and watch
   `GET /candidates` turn `READY`, or re-run the LIVE-TEST block above with
   the demo token.
5. If Support enables only some regions/models, the region and model IDs are
   read from SSM/env (`BEDROCK_MODEL_*` in `src/shared/settings.py`), so a
   swap needs no redeploy; cross-region profiles are listed by
   `aws bedrock list-inference-profiles --region ap-south-1`.

**Truth in figures:** the earlier text said the failure was "The provided model
identifier is invalid" and that Model access could be enabled in the console —
both superseded by the facts above. Sources: AWS re:Post Knowledge Center
"InvokeModel errors" (this exact message → account security restriction →
Support), docs.aws.amazon.com "Request access to models" (first-invocation auto
subscription; the documented setup-window symptom is AccessDeniedException, not
this error),
repost.aws questions QU0jAbLU… and builder.aws.com posts Jun/Sep 2026 (new
accounts, same error, Support-filed, root-user unaffected),
repost.aws QUrHqBJPL… (Free-plan account, zero applied Bedrock quotas).

## Deviations currently active (per the Free-plan profile)

| Item | Active behaviour | Prod path |
|---|---|---|
| Textract | PyMuPDF + Nova Lite vision fallback (verified: text extraction ran in Lambda) | `TEXT_SOURCE=textract` SSM flag, code present |
| S3 Vectors | `s3cosine` fallback (embeddings JSON in S3 + cosine in Lambda); regional CFN types not available there yet | `-c vectorMode=s3vectors` |
| Frontend hosting | S3 static website (verified 200); CORS open to `*` | `-c hostMode=cloudfront` after account verification (AWS Support ticket was the blocker: "Your account must be verified before you can add new CloudFront resources") |
| API edge authorizer | JWT verified **inside** the Lambda (JWKS fetch + client_id/iss/exp checks) | Edge JWT authorizer when aws-cdk-lib supports the spec fields |
| WAF / CMK / PITR | Off in dev | `-c deployWaf=true`, `-c deployCmk=true`, prod profile |

## Commands to know

```
# deploy
cd ai-recruiter-aws/infra && npx aws-cdk@latest deploy -c env=dev --all --require-approval never

# destroy (reclaims everything)
cd ai-recruiter-aws/infra && npx aws-cdk@latest destroy -c env=dev --all --require-approval never

# backend tests (no AWS account needed)
cd ai-recruiter-aws && .venv/Scripts/python.exe -m pytest -q

# frontend deploy
cd ai-recruiter-aws/web && npm run build && aws s3 sync dist s3://ar-dev-web-942893240068 --delete
```

## Known honest limitation

`impl_jobs.start_match` reads `MATCH_MACHINE_ARN` from env — it was added to the
API function's env in the CDK (`pipeline.py`), so `POST /jobs/{id}/match` starts
the Map machine. Verified in the stack definition; not yet exercised live
because matching needs Bedrock access (step 1 above).
