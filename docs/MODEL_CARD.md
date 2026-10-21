# Model card

## Models in use (SSM-swappable, doc §3.4)

| Role | Model | Temperature | Where |
|---|---|---|---|
| Judge (analysis) | Amazon Nova Pro (`amazon.nova-pro-v1`) | 0 | Criteria judging, claim checks, inconsistency detection, screening questions, candidate Q&A, compare summaries |
| Small/fast | Amazon Nova Micro (`amazon.nova-micro-v1`) | 0 | Profile extraction, JD parsing, search-query parsing, role rationale |
| Vision fallback | Amazon Nova Lite (`amazon.nova-lite-v1`) | 0 | Scanned/graphic PDF pages that PyMuPDF cannot read |
| Embeddings | Amazon Titan Text Embeddings V2 (`amazon.titan-embed-text-v2:0`) | — | Resume chunks + queries into S3 Vectors |

## Design guarantees (NFR bounds)

- All model outputs are validated against the JSON schema in
  `prompts/<name>/v.schema.json`; a schema miss retries then fails the step.
- **Total score never comes from a model.** `compute_weighted_total` (in code)
  turns per-criterion scores into the 0-100 total (FR-06, NFR-16).
- Every evidence quote must appear in the source resume text verbatim or with
  ≥90% fuzzy match (NFR-20); unverifiable quotes are dropped and the criterion
  shows "no evidence found".
- Model ID + prompt name + prompt version + rubric weights are stored on every
  analysis (NFR-19), so scores are reproducible and auditable.

## Fairness controls (NFR-17)

- PII suppression before scoring: contact info and protected-attribute phrases
  (gender, age, marital, nationality, DOB) are removed by `redact_pii`.
- Screening questions explicitly exclude protected-attribute categories.
- Human-in-the-loop: the UI labels every AI block "AI-generated, verify before
  use"; no auto-reject anywhere; rubric weights and parsed requirements are
  recruiter-editable, and overrides are stored and used downstream.

## Known limits (honest, per plan §6 deviations)

- Vision OCR fallback reads scanned pages well but is *not* Textract layout
  analysis; very dense multi-column layouts may reorder lines.
- Demos are validated at 100 resumes/10 JDs, not the doc's 5,000-resume scale
  (NFR-03/04 targets documented as designed-but-not-load-proven).
- Grading of partial skill overlap is token-based; a synonym pair the profile
  names in a totally different way degrades to "Partial" instead of "Met".
