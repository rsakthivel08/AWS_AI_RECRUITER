# Plan — Job/match UX restructure

Date: 2026-10-19

## Goal
1. Jobs page: "Open" drawer becomes READ-ONLY — raw JD text + parsed tech-stack requirements only. All candidate-matching UI removed from jobs.
2. Match page: replace "Match against a job description" textarea with a dropdown of registered jobs; AI-scores the active candidate against the selected job using the real match workflow.

## Key findings
- Job items do NOT currently store the pasted JD text; only parsed fields (requiredSkills, mustHaves, seniority, responsibilities...) persist on the job item (verified live in DynamoDB).
- AI never extracts the job title: the parse_jd (v1) prompt's rules omit it, so JobRequirements.title stays null and newly created jobs keep the "(untitled job)" placeholder (see Jobs screenshot).

## Phase 0 — Fix AI job-title extraction
- 0.1 Extend prompts/parse_jd/v1.md: add a rule to extract the job title ("Cloud Engineer", "ML Engineer"...) and emit it as "title".
- 0.2 Update prompts/parse_jd/v1.schema.json so the reply schema includes "title" (string, required).
- 0.3 In src/functions/pipeline/parse_jd_fn.py, after parsing, copy requirements["title"] onto the job item's title when it still the "(untitled job)" placeholder — so existing create_job flow automatically names jobs.
- 0.4 Verify with the real JD that previously produced "(untitled job)": title now appears in the Jobs table.
- Note: jobs created before this fix stay "(untitled job)" unless re-parsed; UI already allows rename via PUT /jobs (optional manual rename).
- JobDetail drawer currently contains the entire Match console (match button, candidate selection, rankings, rubric sliders, compare, CSV export) — all to be removed.
- Match page's JD scoring is a local keyword count + silently creates a throwaway job per click — to be replaced by real POST /jobs/{id}/match + rankings polling.

## Phase 1 — Backend: persist raw JD
- 1.1 create_job stores jdText (≤60k) on the job item — src/functions/api/impl_jobs.py
- 1.2 Check Repo.create_job/update_job field filtering — src/shared/repo.py
- 1.3 Old jobs show no raw JD (no backfill migration)

## Phase 2 — Frontend: jobs read-only drawer
- 2.1 Remove match machinery from JobDetail — web/src/App.tsx
- 2.2 Show: title, status, raw JD block, parsed tech-stack chips (required/preferred/must-haves, seniority)
- 2.3 Delete stays on the jobs list; match/rankings live only in Match page
- 2.4 Prune unused state/handlers in JobDetail

## Phase 3 — Frontend: match page
- 3.1 Load GET /jobs; replace textarea with <select> (disable PARSING jobs)
- 3.2 Score = POST /jobs/{jobId}/match { candidateIds: [activeCandidate] } (202)
- 3.3 Poll GET /jobs/{jobId}/rankings (4s, cap ~60s); show score %/band + per-criterion breakdown with evidence
- 3.4 Delete local keyword scoring + throwaway-job creation (scoreJd body)
- 3.5 Empty state: "No jobs yet — create one in the Jobs tab"

## Phase 4 — Verify
- 4.1 npm run build in web/
- 4.2 pytest backend green
- 4.3 Deploy Lambda; live test: create job → drawer shows JD + tech stack; match page → score appears

## Out of scope
- Editing JD text after creation
- Matching multiple candidates from the Match page (stays one active candidate)
- Backend route changes (jobs/{id}/match, /rankings stay as-is)
