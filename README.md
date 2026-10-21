# AWS AI Recruiter 🤖

> **✅ Try the live app here — no setup needed:**
> # 👉 [**http://ar-dev-web-942893240068.s3-website.ap-south-1.amazonaws.com**](http://ar-dev-web-942893240068.s3-website.ap-south-1.amazonaws.com)
>
> Sign in (or create an account in under 30 seconds), upload a PDF résumé, paste a job description, and watch the AI score, gap-check, compare and rank candidates in real time.

**Region:** ap-south-1 (Mumbai) · **Account:** 942893240068 · **Architecture:** 100 % serverless

---

## What this app does

AI Recruiter is a serverless AI recruiting assistant built entirely on AWS (with a pluggable LLM provider for the dev environment). It lets a recruiter:

| Tab | What it does |
|---|---|
| **Upload** | Drag in a PDF résumé → Step Functions pipeline extracts text, validates & dedups, and produces a structured candidate profile |
| **Candidates** | Browse every processed candidate, status pills showing the pipeline stage, full structured profile per candidate |
| **Jobs** | Paste a raw job description → the LLM parses it into required skills, must-haves, seniority, education, domain |
| **Match** | Score every READY candidate against a job — quick Gemini score chips, then the full per-criterion scoring workflow |
| **Compare** | Head-to-head: two candidates × one job, AI-judged verdict with confidence + deterministic skill-overlap table |
| **Search** | Natural-language candidate search ("Kotlin devs with 3+ years in payments") — AI-ranked top 3 with explanations |
| **Chat** | Conversational interview-prep: AI generates likely interview questions per candidate with model "good answers" |
| **Dashboard** | Pipeline health at a glance — counts by status, recent activity, job stats |

Sign-in is handled by **Amazon Cognito**; every API call carries the Cognito token and the backend scopes everything by user — **two accounts cannot see each other's candidates or jobs** (enforced with tests, see `tests/security/test_isolation.py`).

---

## Architecture in one picture

```
   Browser (React SPA)
     │  hosted by Amazon S3 static website hosting
     ▼
 Amazon Cognito ── signs you in, issues JWT
     ▼
 Amazon API Gateway ── the single HTTPS front door (validates the JWT)
     ▼
 AWS Lambda (8 Python functions)  ◄── also triggered by S3 uploads
     │            │
     │            └── AWS Step Functions: ingest / job / match workflows
     ▼                          ▼
 Amazon DynamoDB         Amazon Bedrock / Gemini / Groq
 (recruiter-dev table)   (scores, comparisons, explainers)
```

A slightly deeper diagram, service-by-service Console click-paths, and a 5-minute demo script live in **[docs/README-for-professor.md](docs/README-for-professor.md)** — written for someone brand-new to AWS.

---

## Repo layout

```
infra/          AWS CDK app — defines every cloud resource in code
  app.py        the CDK entry point
  stacks/       storage, data, auth, pipeline, api, frontend, observability
  layer/        the shared Python Lambda layer (PyMuPDF, pydantic, google-genai…)
src/functions/  Lambda handlers (pipeline + API)
src/shared/     domain library (models, repo, AI-provider factory, vector store, guards)
prompts/        versioned prompt templates + JSON schemas (parses_jd, judge_criterion…)
data/           taxonomy seed lists + 32 job roles
web/            React + Vite + TypeScript SPA
tests/          unit (moto — no AWS account needed) + security (data-isolation) suites
scripts/        deploy_web.py / deploy_lambdas.py / test_api.py helpers
docs/           ARCHITECTURE.md · RUNBOOK.md · MODEL_CARD.md · README-for-professor.md
.github/        CI/CD workflows (dev on merge, prod on tag, OIDC)
```

## Getting the code running locally

```bash
# 1. the web SPA
cd web
npm install
cp .env.example .env.local   # then fill in VITE_* values (Cognito + API URL + Gemini key)
npm run build                # or: npm run dev (vite dev server)

# 2. deploy the backend (requires AWS credentials)
pip install -r infra/requirements.txt -r infra/requirements-dev.txt
cd infra
npx aws-cdk deploy -c env=dev --all

# 3. tests (no AWS account needed — moto mocks AWS)
pytest -q
```

### Notes for a fresh deployment

- **`.env.local` in `web/` is git-ignored on purpose.** It carries `VITE_USER_POOL_ID`, `VITE_CLIENT_ID`, `VITE_COGNITO_DOMAIN`, `VITE_API_URL`, and `VITE_GEMINI_API_KEY` — real values from your own deploy, not from the repo.
- **API keys are NOT stored in this repository.** `scripts/deploy_lambdas.py` and `scripts/env_*.json` ship with placeholders; put real keys in SSM Parameter Store (`/ai-recruiter/dev/…`) or set them when deploying. Both a Groq and a Gemini fallback for the main AWS Bedrock path are supported via the `AI_PROVIDER` setting in `src/shared/ai_factory.py`.
- `infra/layer/` (the Lambda dependency layer) is checked in as-is so CDK deploys reproduce byte-for-byte; 100 MB+ files inside it are filtered by `.gitignore`.

## CI / CD

`.github/workflows/` contains:
- `ci.yml` — typecheck + pytest + web build on every push
- `deploy.yml` — OIDC-based AWS deploy on merges to main

---

## Documentation

| File | What's inside |
|---|---|
| [docs/README-for-professor.md](docs/README-for-professor.md) | ⭐ **Beginner AWS walkthrough** — which service does what in the app, where to click it in the AWS Console, plus a 5-minute live demo script |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Technical design decisions |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Operational runbook — deploy, secrets, migration |
| [docs/MODEL_CARD.md](docs/MODEL_CARD.md) | AI model usage, prompts, limitations |

---

## Security notes (please read before reusing)

- Never commit real API keys or `.env.local` (see `.gitignore`)
- Candidate résumés are personal data; the sample PDF under `web/public/` is synthetic
- Frontend `VITE_*` vars are public by design — put anything private in the backend only
