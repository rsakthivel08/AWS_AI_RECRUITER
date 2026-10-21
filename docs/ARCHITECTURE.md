# Architecture — AWS AI-Recruiter (serverless rework)

Follows "AWS Cloud Rework — Project Requirements Document" (Oct 13, 2026).

## Components

```
Browser (React SPA on CloudFront)
        │  Cognito JWT
        ▼
API Gateway HTTP API (+JWT authorizer, throttling)  ──┐
                                                      │
uploads/{sub}/resumes|jobs (presigned PUT, 5 min) ──────┐
        │                                               │
   S3 ObjectCreated ──▶ ingest_trigger Lambda           │
        │                                               │
   Step Functions  ingest / job / match  ───────────────┤
        │   validate_dedup ─ extract_text ─ extract_profile ── score_candidate ── finalize_ranking
        │   parse_jd
        ▼
S3 (text/, vectors/, exports/) + S3 Vectors (Titan V2) + DynamoDB (single table, gsi1 ranking, gsi2 dedup)
```

## Documented deviations from the requirements doc (Free-plan profile)

| Doc item | Shipped behaviour | Section |
|---|---|---|
| Textract | NOT billing-active. `TEXT_SOURCE` SSM flag; default `freepdf` = PyMuPDF text layer with Nova Lite vision OCR fallback on low-text pages. `extract_text._textract_start_and_wait` is the ready-but-dormant Textract path. | plan §2 |
| NFR-08 customer-managed KMS | SSE-S3 default; CMK rendered behind `-c deployCmk=true`. | plan §2 |
| NFR-12 WAF | Rate limiting enforced at API Gateway throttles + presign validation. WAF ACL rendered behind `-c deployWaf=true`. | plan §2 |
| NFR-03/04 scale | 5 concurrency cap on the match Map = Bedrock quota guardrail. Load-validated at demo scale. | plan §6 |

## DynamoDB layout (doc §3.3)

| Item | PK | SK | Notes |
|---|---|---|---|
| Candidate | `U#<sub>` | `C#<candId>` | profile, status, sha256, S3 key |
| Job | `U#<sub>` | `J#<jobId>` | parsedRequirements, rubricWeights |
| Analysis | `U#<sub>` | `A#<jobId>#<candId>` | criteria+gaps+claims+flags+questions, `gsi1pk/gsi1sk` for ranking |
| Role suggestion | `U#<sub>` | `R#<candId>` | top-5 roles with rationale |
| Usage | `U#<sub>` | `USAGE#YYYY-MM-DD` | daily match limit (NFR-25) |

GSI1: rank per job (score zero-padded descending). GSI2: dedup by `sha256` per user.

## API surface (doc §4.3)

Exactly the 17 routes listed in the doc: presign, extract, candidates (list/get/delete/ask), jobs CRUD + parse + match + rankings + analysis + export, search, compare. One error shape `{code, message, requestId}`; `202` for async starts.

## Scoring (doc §4.2)

Per rubric criterion, evidence retrieval via S3 Vectors top-3 chunks → Nova Pro judges 0-100 with quoted evidence → quotes verified ≥90% fuzzy against the source resume (NFR-20), else criterion is marked "no evidence found" → weighted total computed in Lambda (never by the model).

Defaults: requiredSkills 35, relevantExperience 25, seniority 10, preferredSkills 10, educationCerts 10, domainFit 10. Bands: ≥75 Strong, 55-74 Good, 35-54 Partial, <35 Weak. Must-haves missing → flag, never auto-reject.
