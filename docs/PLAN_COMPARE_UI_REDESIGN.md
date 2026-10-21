# Plan — "Compare Candidates" section + Professional UI / Animation upgrade

> Status: **PROPOSAL — awaiting approval.** Nothing below has been implemented yet.

## 0. Summary

Two workstream, both **frontend-only** (no Lambda / DynamoDB / state-machine changes):

| # | Workstream | Touches |
|---|------------|---------|
| A | New **Compare Candidates** tab — pick 2 candidates + 1 job, Groq `openai/gpt-oss-20b` (same model as Search) produces a tabular strengths/weaknesses comparison + verdict | `web/src/App.tsx`, `web/src/App.css` |
| B | **Professional UI upgrade** — theme engine ( selectable palettes ) + advanced micro-interaction animations | `web/src/App.css`, small hooks in `App.tsx` |

Existing foundations that make this cheap:
- Tabs are a simple `{ key, label }[]` + `page === key` render switch — one entry to add.
- `GET /candidates` (READY-filtered) and `GET /jobs` already exist and CORS is allowed.
- Search already demonstrates the client-side Groq pattern (`VITE_GROQ_API_KEY`, model `openai/gpt-oss-20b`, temperature 0.2, JSON-only reply).
- `App.css` is a **CSS-variable design system** with spring/out easing tokens, page-transition, pulses, etc. Theming = emitting a second variable block.

---

## 1. Workstream A — Compare Candidates

### 1.1 UX flow (in-page, 3 steps with visible stepper)

1. **Select step** — three selector cards:
   - **Candidate A** — dropdown over READY candidates (name + role chips).
   - **Candidate B** — same list, candidate A's id greyed out.
   - **Job** — dropdown over existing jobs (title + seniority chip).
   - "Swap A ↔ B" ghost button; validation: A ≠ B, both READY, job selected.
2. **Comparing step** — skeleton shimmer + staged progress ("Gathering profiles → Judging against job → Building table") driven by the in-flight request, not fake timers.
3. **Result step** — four result blocks (below).

### 1.2 Model prompt (client-side, same pattern as Search)

- Endpoint: `https://api.groq.com/…/chat/completions`, `model: openai/gpt-oss-20b`, temperature `0.2`, JSON-only system message (same hard style as Search).
- Context payload per candidate = same compact summary builder Search uses (name, skills/tools/languages, top roles, summary) **plus** the job's title, requirements list and preferred skills (parsed from the Job item exactly the way the Match screen reads a job).
- **No PII beyond what Search already sends**; no resume text (the SPA doesn't fetch the text objects — Match data only if an analysis row exists: see 1.3).

### 1.3 Response contract (strict JSON, validated + normalized in code)

```jsonc
{
  "table": [
    // one row per comparison dimension; model chooses 5-7 rows
    { "dimension": "Backend depth", "a": "…", "b": "…", "advantage": "A" | "B" | "tie" }
  ],
  "strengths":  { "a": ["…","…"], "b": ["…","…"] },
  "weaknesses": { "a": ["…","…"], "b": ["…","…"] },
  "verdict": {
    "winner": "A" | "B" | "tie",
    "confidence": "high" | "medium" | "low",
    "explanation": "3-5 sentence justification anchored to the job requirements."
  }
}
```

Defensive parsing (in code, not the model): strip markdown fences, `JSON.parse` with try/catch, validate the `advantage`/`winner` enums (`"A"/"B"` invalid → coerced to `"tie"`), arrays coerced to `[]`, missing `verdict.explanation` → fallback sentence. Any hard failure → user-visible error banner, never a partially rendered table.

### 1.4 Result rendering

- **Comparison table** — the core deliverable. Rows stagger-animate in; the `advantage` column renders a winner badge (▲ A / ▲ B / —) with the cell contents tinted on the favoured side. Sticky header.
- **Strengths / weaknesses twin columns** — A in a blue-tinted card, B in a violet-tinted card; each chip slides in with a 40 ms stagger.
- **Skills overlap strip** computed **in code** (never by the model): symmetric-diff of A/B skills, neutral = shared, tinted = candidate-unique. Adds a deterministic anchor the model can't hallucinate.
- **Final verdict card** — large "Winner" banner (animated gradient border sweep), confidence meter (animated bar), explanation paragraph. If `verdict.winner === "tie"`, renders a balanced "Both viable, here's the tie-break" layout instead of a winner badge.
- MoM (per-job) analyses are shown as small "Scored job context" chips (score % + band) under each candidate name **when they exist** — they inform, they don't replace the comparison.

### 1.5 Edge cases

| Situation | Behavior |
|---|---|
| < 2 READY candidates | Selectors disabled with hint; error banner explains |
| No jobs yet | Job selector empty hint → "Add a job first" |
| A == B | Compare disabled; inline validation text |
| No `VITE_GROQ_API_KEY` | **Graceful fallback** (same spirit as Search): local heuristic comparison built from the profile diffs (shared skills, role overlap, requirement keyword hits) with an explicit "AI judge unavailable — heuristic comparison" banner. No crash. |
| Groq 429 / network error | Error banner, retry button; staged progress resets |
| > 2 candidates wanted later | Table is dimension-row based, so N-candidate extension is additive (out of scope now) |

### 1.6 Files touched (A only)

- `web/src/App.tsx` — `<Compare />` component + `Page` tab entry (+ ~350 lines).
- `web/src/App.css` — new section: `.compare-*` classes (table, verdict, stepper).
- No infra, no backend, no new dependencies.

---

## 2. Workstream B — UI upgrade

### 2.1 Theme engine

- Add `data-theme` attribute on `<html>`; a small theme registry maps each palette name → CSS variable overrides (all 14 existing tokens keep their roles: surfaces, borders, text tiers, accent + good/warn/bad, glow).
- **Theme picker**: small palette swatch row inside the nav (tooltip labels) — persists per browser via `localStorage`, no auth/profile storage involved.
- Because every existing class consumes the variables, **all current screens instantly adopt the chosen theme** — the new Compare screen included.

### 2.2 Animation upgrades (pure CSS — no new deps, matching the project's "no external deps" rule)

- **Page transitions**: directional slide/fade in the existing `page-in` keyframe (direction-aware: forward vs backward tab switching), plus a tab-underline that **slides** between tabs instead of re-animating per tab.
- **List entrance**: `@starting-style`-style staggered fade-up on card lists (Candidates/Jobs/Search/Compare), 40 ms per-item delay.
- **Interactions**: hover lift on cards (`translateY(-2px)` + soft spotlight following cursor via `--mx/--my` radial-gradient), button ripple/press scale already present — extend to checkbox/chip toggles with spring pop.
- **Comparing state**: skeleton shimmer (existing `.spinner` upgraded to shimmer blocks), staged progress bar with moving gradient.
- **Result state**: table rows cascade-in, winner banner gradient sweep, confidence bar animates from 0 to value, dims/`prefers-reduced-motion` respected via one `@media` guard.
- All animations GPU-only (`transform`/`opacity`) — no layout thrash.

---

## 3. Theme catalogue (pick one singer-pair letter)

All are dark-first to keep the existing "premium dark" character; #6 is the one light option.

| # | Theme | Surfaces (bg-0→bg-2) | Accent(s) | Meaning colors (good/warn/bad) | Character |
|---|-------|---------------------|-----------|-------------------------------|-----------|
| **T1** | **Obsidian Indigo** *(current, refined)* | `#0a0d14 / #10141f / #161c2b` (unchanged) | periwinkle `#7aa2ff` + violet `#9d7aff` | mint `#4ade9b` / gold `#ffcc66` / coral `#ff7a8a` | The system default; refined slightly (subtle stagger + spotlight only) |
| **T2** | **Midnight Emerald** | slate `#0b1210 / #10201a / #16281f` | emerald `#34d399` + champagne gold `#e6c676` | mint/gold/soft red — kept | Trustworthy "finance/enterprise" feel; great for verdicts |
| **T3** | **Nebula Violet** | aubergine `#0d0a14 / #14101f / #1c1628` | magenta-violet `#c084fc` + cyan `#67e8f9` accents | melon/magenta-warning/desat-red | Bold "AI-native product" look, most dramatic animations |
| **T4** | **Carbon Amber** | warm charcoal `#121212 / #191817 / #221f1c` | amber `#f59e0b` + copper `#fb923c` (monochrome UI, single hot accent) | green/amber/deep-red — semantics align | Industrial, high-contrast, zero "startup neon" |
| **T5** | **Cyber Teal** | deep abyss `#06120f / #0a1d18 / #0f2a23` | teal `#2dd4bf` + electric lime `#a3e635` highlights | lime/warn-yellow/red | Techy/recruiter-dashboard energy |
| **T6** | **Porcelain Blue** *(light mode)* | `#f7f9fc / #ffffff / #eef3fa` | Executive blue `#2563eb` + slate `#475569` | green-600/amber-600/red-500 | Classic SaaS light theme; also proves the theming works both ways |

> Heap recommendation if you want my bias: **T2 Midnight Emerald** for the hiring/human-verdict feel, **T3 Nebula Violet** if you want the flashiest animated demo. T1 stays default until you pick.

---

## 4. Scope guard-rails (unchanged by this plan)

- Owners stay implied by `U#<sub>` — candidates/jobs remain **per-user scoped** (Compare lists only the signed-in user's rows; Compare introduces no new storage or API).
- Groq key stays a client-side `VITE_` var *consistent with Search for now*; backend proxying (SSM/Secrets → Lambda relay) is noted as a **future hardening step**, recommended but explicitly out of this plan's scope unless you say otherwise.

---

## 5. Delivery order + verification

1. Theme engine + selected palette → build, eyeball every existing tab (nothing may regress).
2. Compare section on top of the themed shell (it inherits the theme automatically).
3. `npm run build` in `web/` (tsc + vite — type safety), no Python/infra tests affected (zero backend changes).
4. `aws s3 sync web/dist s3://ar-dev-web-942893240068 --delete` — same channel as previous iterations; no serverless deploy needed.
5. Live-verify in the browser panel: pick 2 READY candidates + a job → full table/verdict flow; then the fallback path by temporarily unsetting the key.

No limitations: not yet implemented — waiting on your go + theme pick.
