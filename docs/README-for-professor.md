# AI Recruiter — A Beginner's Guide to the AWS Behind It

This document explains, in plain language, **which AWS services power each section of the AI Recruiter app** and — just as important — **where you can click in the AWS Console to see each one yourself**. No prior AWS knowledge is assumed.

**One-time setup to follow along:** sign in at <https://console.aws.amazon.com> with the account that owns this project (account ID `942893240068`, region **ap-south-1 / Asia Pacific Mumbai**). At the very top-right of the console, switch the region dropdown to **Asia Pacific (Mumbai) ap-south-1** — if you're in another region you'll see empty lists and think something is missing, when it's just in Mumbai.

---

## 1. What happens when you use the app (the big picture, 60 seconds)

```
    YOU (browser)
      │  sign in                ──►  Cognito      (the doorman — issues a login token)
      │  every button click     ──►  API Gateway  (the front desk — one stable HTTPS URL)
      │                                │
      │                                ▼
      │                          Lambda (AWS's "rent-a-server" — Python functions run)
      │                                │
      │                ┌───────────────┼────────────────┐
      │                ▼               ▼                ▼
      │           DynamoDB         Step Functions     Gemini/Groq APIs
      │        (the database)   (workflow managers)  (the AI brain)
      │
      └─ the SPA itself lives ─► S3 (simple file storage that serves the website)
```

Everything after "API Gateway" happens only when a button is pressed — there is **no always-on server you pay for**.

---

## 2. The front end — what you see in the browser

| | |
|---|---|
| **Section of the app** | The web pages themselves: sign-in, Dashboard, Match, Chat, Candidates, Jobs, Search, Compare, Upload |
| **AWS service** | **Amazon S3 — Static Website Hosting** |
| **Where to see it (Console)** | left sidebar **S3 → Buckets → `ar-dev-web-942893240068` → Properties tab → scroll to "Static website hosting" → Bucket website endpoint** |
| **What to point out to a professor** | The entire React app is *just files* (one `index.html`, a `.js` bundle, one `.css`) in a bucket. Click the endpoint link and the app opens. Under the **Objects tab** you can literally see the files. Currently: **no servers hosting the site at all.**

The source code that builds those files lives in this repo under [`web/`](../web/); `npm run build` produces the `dist/` folder, and [`scripts/deploy_web.py`](../scripts/deploy_web.py) uploads it to the bucket.

---

## 3. Sign-in & accounts

| | |
|---|---|
| **Section of the app** | The "AI Recruiter" sign-in email + password screen; every user's identity |
| **AWS service** | **Amazon Cognito (User Pool)** |
| **Where to see it (Console)** | left sidebar **Amazon Cognito → User pools → `ar-dev-recruiter`** (approximate name; the domain used by the app is `ar-dev-recruiter.auth.ap-south-1.amazoncognito.com`) |
| **What to point out** | **Users tab** — every person who has signed up is a row here; passwords are hashed, never stored in the app. Cognito also issues the **short-lived token** the app attaches to every API call, so the backend can tell *who* is asking. **App client tab** shows the client ID the web app was configured with. |

In the app: only signed-in users can reach any tab, because the token is required by the API.

---

## 4. The API layer — the single HTTPS front door

| | |
|---|---|
| **Section of the app** | Everything: every button — upload, list candidates, run match, compare, search |
| **AWS service** | **Amazon API Gateway (REST API)** |
| **Where to see it** | left sidebar **API Gateway → APIs → "ai-recruiter-…dev…" → Resources** |
| **What to point out** | You'll see routes (`/candidates`, `/jobs`, `/uploads`, `/search`, …). Under **Stages → dev** is the invoke URL the web app is actually configured with: `https://gvmmouvwtf.execute-api.ap-south-1.amazonaws.com` (from `web/.env.local` `VITE_API_URL`). API Gateway **forwards the Cognito token** to Lambda automatically and rejects requests without one. |

---

## 5. The compute — where the code actually runs

| | |
|---|---|
| **Section of the app** | Every action, behind the scenes |
| **AWS service** | **AWS Lambda** (Python functions) |
| **Where to see it** | left sidebar **Lambda → Functions** — you'll find 8 functions, all prefixed `ai-recruiter-dev-backend-…`. Test one by clicking it → **Test tab** → create a simple template event → Test. **Monitor → Logs** shows Python `print`/logger output live. |
| **What to point out** | **Lambda = renting a function, not a server.** Each function runs only when a web request or workflow step arrives, sleeping in between. Under the **Configuration → Environment variables** tab you can see settings each one uses (table name, bucket names, which AI model). The functions map *1-to-1* to app actions: |

| Lambda function | Drives this UI section |
|---|---|
| `…apiC8550315…` | main API router: dashboard stats, candidates, jobs, chat, compare, search |
| `…parseJd9EE5AA46…` | **Jobs tab** — paste a job description → Gemini extracts required skills/seniority into JSON |
| `…extractProfile0A57264A…` | **Candidates tab** — turn raw resume text into a structured profile |
| `…extractText4BFE7A68…` | **Upload tab** — pull text out of a PDF when the bytes arrive in S3 |
| `…validateDedup15E07D80…` | prevents duplicate resumes / sanity-checks fields |
| `…scoreCandidate3E6DF976…` | **Match tab** — scores one candidate against one job |
| `…finalizeRankingD58F5EB5…` | compiles per-candidate scores into a ranked list |
| `…ingestTriggerEE3DFA6B…` | the domino that fires the whole upload workflow (see §6) |

---

## 6. The workflows — how multi-step jobs stay organized

| | |
|---|---|
| **Section of the app** | **Upload tab** (process a resume end-to-end) and **Match tab** (score a full pipeline) |
| **AWS service** | **AWS Step Functions** |
| **Where to see it** | left sidebar **Step Functions → State machines** — three machines: `ar-dev-ingest`, `ar-dev-job`, `ar-dev-match`. Click **`ar-dev-ingest`**, then any execution in the list: you'll get the famous **graph view**, a colored flowchart of every step, green = succeeded. Click inside a state to see its exact input/output JSON. |
| **What to point out** | Step Functions is **a state machine** — a resumable, visual orchestrator: "extract text → validate & dedup → extract profile → score candidate → finalize ranking," with automatic retries and error handling so a flaky step doesn't corrupt the run. This is the single most "wow, it's like a flowchart came alive" demo for a professor: **run one upload in the app, refresh this console page, watch the execution appear mid-flight.** |

---

## 7. The database

| | |
|---|---|
| **Section of the app** | all of Candidates, Jobs, Match results, Chat history — the data itself |
| **AWS service** | **Amazon DynamoDB** (NoSQL) |
| **Where to see it** | left sidebar **DynamoDB → Tables → `recruiter-dev` → Explore table items** |
| **What to point out** | Each row is a flexible JSON-like "item." Show the **partition key** (whose data it is) and **sort key** (what kind of record) by opening the table index. Rows are tagged by user (`pk`), so **user A literally cannot see user B's data** — there's a test suite (`tests/security/test_isolation.py`) that proves it. |

---

## 8. Storage for uploaded files

| | |
|---|---|
| **Section of the app** | **Upload tab** — the actual PDF you drag in |
| **AWS service** | **Amazon S3** (regular object storage, not the website bucket) |
| **Where to see it** | **S3 → Buckets** — several, by purpose: |
| | `ar-dev-uploads` — incoming PDFs; clicking an object → preview shows the actual resume. Note: **the act of a file arriving here is what triggers the whole pipeline** (§6) |
| | `ar-dev-vectors` — AI-embedding vectors used by the "Search" page to find similar candidates semantically |
| | `ar-dev-exports` — any CSV/PDF exports the app generates |
| **What to point out** | S3 = "bottomless hard drive with URLs." Different buckets literally separate *files that are data* from *files that are code* (the website bucket from §2). Check **Properties → Event notifications** on `ar-dev-uploads` to see the "new file → notify Lambda → start the state machine" wiring. |

---

## 9. The AI itself

| | |
|---|---|
| **Section of the app** | Job parsing, resume profile extraction, match scoring, the Compare "AI judge", the Search explainer, Chat |
| **AWS service (primary)** | **Amazon Bedrock** — AWS-managed frontier models (`amazon.nova-pro-v1` for the judge, `nova-micro` for quick tasks, `amazon.titan-embed-text-v2:0` for semantic embeddings) |
| **Where to see it** | left sidebar **Amazon Bedrock → Model access / Playgrounds**. **Playgrounds → Chat** lets you chat with Nova Pro yourself in the console, no code. |
| **Status note (fair to disclose)** | This dev account's Bedrock quota is currently on hold, so, per the abstraction in [`src/shared/ai_factory.py`](../src/shared/ai_factory.py), the same back-end calls are silently taking the **Google Gemini** / **Groq** fallback path with *identical prompts and output contracts* — `AI_PROVIDER=gemini` in each Lambda's environment variables sets it (visible in **Lambda → function → Configuration → Environment variables**). Nothing about the app changes; only the model endpoint does. |

---

## 10. Configuration & secrets

| | |
|---|---|
| **Section of the app** | invisible, but everything depends on it |
| **AWS service** | **AWS Systems Manager (Parameter Store)** and **CloudWatch Logs** |
| **Where to see it** | **Systems Manager → Parameter Store** — parameters under `/ai-recruiter/dev/…` (keys/config), out of source code. **CloudWatch → Log groups** — one per Lambda; open one → **Log streams** → you'll see the exact JSON a request handled, timestamped. This is the console place to **prove the pipeline did what it says**: do an upload in the app, then refresh the log stream. |
| **Infrastructure-as-code** | everything is built from CDK in [`infra/app.py`](../infra/app.py) — **CloudFormation → Stacks** shows the generated resource tree for the whole app in one place. |

---

## 11. Cheat-sheet: one line per service

| AWS service | In one phrase | Console path (region: ap-south-1) |
|---|---|---|
| **S3** | bottomless file storage + free static website hosting | S3 → Buckets (`ar-dev-web…`, `ar-dev-uploads`, `ar-dev-vectors`, `ar-dev-exports`) |
| **Cognito** | sign-up / sign-in / session tokens | Cognito → User pools |
| **API Gateway** | the one HTTPS front door every click hits | API Gateway → APIs |
| **Lambda** | rent-a-function compute (8 Python functions) | Lambda → Functions |
| **Step Functions** | visual state-machine orchestrator | Step Functions → State machines (`ar-dev-ingest/job/match`) |
| **DynamoDB** | serverless NoSQL database | DynamoDB → Tables → `recruiter-dev` |
| **Bedrock** | managed AI models (Nova family); Gemini/Groq as dev fallback | Bedrock → Playgrounds |
| **CloudWatch** | logs & alarms | CloudWatch → Log groups |
| **Systems Manager** | config & secrets | Systems Manager → Parameter Store |
| **CloudFormation/CDK** | infrastructure defined in code | CloudFormation → Stacks |

---

## 12. A recommended 5-minute live demo script (for your professor)

1. **Open the site** from the S3 bucket website endpoint (§2): no servers visible anywhere.
2. **Upload a PDF resume** in the Upload tab.
3. **Within seconds**, open Step Functions → `ar-dev-ingest` → refresh: a new colored execution diagram appears, step by step, finishing green (§6).
4. **Open DynamoDB → Explore items**: your resume is now structured JSON (§7).
5. **Open CloudWatch Logs** of the Lambda that ran: you can see each step's raw input/output (§10).
6. **In the app, run a Match**: function scores the candidate against a job via Gemini/Bedrock (§5+§9).
7. **In the app, hit Compare**: two candidates, one AI judge verdict, back-to-back.

Enjoy the demo — and if your professor asks *why* this design, the answer is: **every component here is serverless (pay-per-use, auto-scales, no ops burden), each one does exactly one thing, and every one of them is inspectable in a console you can point at.**
