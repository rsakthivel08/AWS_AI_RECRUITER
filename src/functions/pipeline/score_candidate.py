"""Match-workflow Map step (FR-05 to FR-11): one candidate scored against one job.

Per candidate and job:
1. Retrieve evidence per JD requirement (top-3 chunks, owner-scoped vectors).
2. Judge each rubric criterion (Nova Pro, temperature 0, quoted evidence) — the
   quotes are verified against the stored resume text; unverified quotes are
   dropped and the criterion shows "no evidence found" (NFR-20).
3. Compute the weighted total in code, never in the model (FR-06).
4. Skill-gap report (Met/Partial/Missing + severity + transferable) determined
   deterministically from the profile and requirement text (FR-07).
5. Claim checks (FR-08) and inconsistency flags (FR-08b) via Nova Pro.
6. Screening questions grounded in gaps/claims/flags, default 8 (FR-11).
Writes one Analysis item; the final workflow step writes the GSI1 ranking view.
"""
from __future__ import annotations

import json
import re

from src.shared.guards import INJECTION_PREAMBLE, redact_pii, wrap_untrusted
from src.shared.keys import DEFAULT_RUBRIC, compute_weighted_total
from src.shared.logging import get_logger
from src.shared.models import (
    Analysis,
    ClaimsOut,
    InconsistenciesOut,
    JobRequirements,
    QuestionsOut,
    ScoreCriteriaOut,
)
from src.shared.prompt_loader import load_prompt, record_version, render
from src.shared.repo import Repo
from src.shared.settings import load_settings
from src.shared.vector_store import VectorStore

LOG = get_logger("score-candidate")


def handler(event: dict, context: object) -> dict:
    """Step Functions Map input: {userSub, candidateId, jobId, rubricWeights}."""
    settings = load_settings()
    repo = Repo(settings.table_name, region=settings.aws_region)
    sub, cid, jid = event["userSub"], event["candidateId"], event["jobId"]

    candidate = {k: v for k, v in (repo.get_candidate(sub, cid) or {}).items()}
    job = repo.get_job(sub, jid)
    if not candidate or not job:
        return {"failed": True, "reason": "missing candidate or job",
                "candidateId": cid, "jobId": jid}
    resume_text = _resume_text(settings, cid)
    # Parsed requirements live TOP-LEVEL on the job item (parse_jd_fn persists
    # them flat via update_job); there is no nested "parsedRequirements" dict.
    jd = JobRequirements.model_validate(
        {k: v for k, v in job.items() if k in JobRequirements.model_fields})
    raw_weights = job.get("rubricWeights") or DEFAULT_RUBRIC
    # DynamoDB numbers arrive as Decimal; everything downstream (json.dumps of
    # the rubric, GSI sort keys) needs plain ints.
    weights = {k: int(v) for k, v in raw_weights.items()}
    profile = candidate.get("profile", {})

    criteria, evidence_all = _judge_criteria(settings, jd, weights, resume_text, sub, cid)
    total = compute_weighted_total(criteria)
    gaps = _derive_gaps(jd, profile)
    claims = _check_claims(settings, resume_text, profile.get("claims", []), jd.title)
    flags = _detect_inconsistencies(settings, resume_text, claims)
    for must_have in jd.mustHaves:
        # mustHaves may be strings, {"skill": years} maps or lists — normalise
        # to a searchable requirement string instead of indexing the container.
        if isinstance(must_have, dict):
            req = "; ".join(str(k) for k in must_have)
        elif isinstance(must_have, (list, tuple)):
            req = "; ".join(str(m) for m in must_have)
        else:
            req = str(must_have)
        if req and _missing(req, profile):
            flags.append({"type": "mustHave", "detail": f"Missing must-have: {req}"})
    questions = _screening_questions(settings, jd, gaps, claims, flags)

    analysis = Analysis(
        candidateId=cid, jobId=jid, totalScore=total,
        criteria=criteria, gaps=gaps,
        claimChecks=claims.get("claimChecks", []),
        flags=[f for f in InconsistenciesOut.model_validate({"flags": flags}).flags],
        questions=questions.get("questions", [])[:12],
        modelId=(getattr(settings, "groq_model_text", None) or "openai/gpt-oss-20b")
                if (getattr(settings, "ai_provider", None) or "bedrock").lower() == "groq"
                else settings.bedrock_model_judge,
        promptVersion="v1",
    ).model_dump()
    analysis.update(record_version("judge_criterion", "v1"))
    repo.put_analysis(sub, analysis)
    return {"scored": True, "candidateId": cid, "jobId": jid, "totalScore": total}


# ---------- wiring helpers ----------------------------------------------------------

def _resume_text(settings: object, cid: str) -> str:
    import boto3

    s3 = boto3.client("s3", region_name=settings.aws_region)
    key = f"text/{cid}.txt"
    try:
        return s3.get_object(Bucket=settings.text_bucket, Key=key)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        # No extracted text (upload deleted / extraction failed)? Score with
        # no evidence instead of cracking the whole match run.
        LOG.warning("no extracted resume text for candidate", extra={"extra_fields": {"candidateId": cid}})
        return ""


def _judge_criteria(settings: object, jd: JobRequirements, weights: dict,
                    resume_text: str, sub: str, cid: str) -> tuple[list[dict], list[dict]]:
    """sub = owner (U#-level sub), cid = candidate being scored.

    Note: retrieval MUST be scoped sub/cid separately — passing the cid as the
    owner prefix silently returns zero chunks (vectors/<cid>/ does not exist).
    """
    from src.shared.ai_factory import AIFactory

    vectors = VectorStore(settings)
    bedrock = AIFactory.for_settings(settings)
    template, _ = load_prompt("judge_criterion", "v1")
    retrieved_chunks = vectors.query(sub, json.dumps(jd.model_dump(), ensure_ascii=False),
                                     top_k=3, candidate_id=cid)
    # Retrieval that comes up empty (no vectors uploaded, S3 fallback miss)
    # must not starve the judge: fall back to the extracted resume text itself.
    if not retrieved_chunks and resume_text.strip():
        fallback = [resume_text[i:i + 1500] for i in range(0, len(resume_text), 1500)]
        retrieved_chunks = [{"text": t, "score": 1.0} for t in fallback[:3]]
    redacted_chunks = redact_pii("\n---\n".join(c["text"] for c in retrieved_chunks))

    # NFR-17: PII is stripped from the text sent to the judge; chunk structure kept.
    chunks = [{"text": c, "score": 1.0, "candidateId": sub, "chunkIndex": i}
              for i, c in enumerate(redacted_chunks.split("\n---\n"))]

    criteria_out, evidence_all = [], []
    #
    # One batched call per candidate (NFR-03): all rubric criteria are judged in a
    # single generation instead of one call per criterion. With N concurrent Map
    # lanes (concurrency 5) and 6 rubric criteria each, per-criterion calls burst
    # 5x6 = 30+ concurrent generations and trip the provider's rate limit; the
    # batches bring it to 5 concurrent calls. Weights stay in code (FR-06).
    rubric_json = json.dumps([{"name": n, "weight": w} for n, w in weights.items()], ensure_ascii=False)
    user = render(template,
                  requirement_json=json.dumps(jd.model_dump(), ensure_ascii=False),
                  rubric_json=rubric_json,
                  evidence_chunks=wrap_untrusted("RESUME_EVIDENCE", "\n---\n".join(
                      c["text"] for c in chunks)))
    data = bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                 model_id=settings.bedrock_model_judge)
    by_name: dict[str, dict] = {}
    rows = _as_list(data).get("criteria", []) or _as_list(data).get("criteriaRows", []) or []
    # Multi-criterion replies may come back keyed differently (e.g. top-level
    # list or "scores") — normalise defensively.
    if not rows and isinstance(data, list):
        rows = data
    for row in rows:
        if isinstance(row, dict) and row.get("name"):
            by_name[str(row["name"]).strip().lower()] = row
    for name, weight in weights.items():
        r = by_name.get(name.lower()) or {}
        criteria_rows = [{
            "name": name,                                  # rubric's own name
            "weight": weight,                              # rubric's own weight
            "score": int(r.get("score", 0)),
            "evidence": [e for e in (r.get("evidence") if isinstance(r.get("evidence"), list) else [])
                         if isinstance(e, dict) and e.get("quote")],
        }]
        parsed = ScoreCriteriaOut.model_validate({"criteria": criteria_rows})
        criterion = parsed.criteria[0]
        kept = []
        for ev in criterion.evidence:
            if ev.quote and any(
                    _fuzzy(ev.quote, c["text"]) >= 0.9 for c in chunks):
                kept.append(ev.model_dump())
        criterion.evidence = kept  # NFR-20: unverifiable quotes are dropped
        criteria_out.append(criterion.model_dump())
        evidence_all.extend(kept)
    return criteria_out, evidence_all


def _as_list(data: dict) -> dict:
    """Accept either {'criteria': [...]} or a single criterion dict."""
    if "criteria" not in data:
        return {"criteria": [data]}
    return data


def _fuzzy(quote: str, source: str) -> float:
    import difflib

    q = " ".join(quote.lower().split())
    s = " ".join(source.lower().split())
    if not q:
        return 0.0
    if q in s:
        return 1.0
    return difflib.SequenceMatcher(None, q, s).ratio()


# ---------- gaps (FR-07) ------------------------------------------------------------

# Curated near-synonym / adjaceny pairs: fallback when the profile names a skill
# in a different way than the JD requirement (doc section 2.2 — "CI/CD pipelines"
# vs "Jenkins"). The judge model handles this semantically; this map is the
# deterministic fallback used for the gap report where the LLM's gap output is
# not yet considered robust enough to replace token logic (doc section 4.2).
TRANSFER_MAP: dict[str, tuple[str, ...]] = {
    "kubernetes": ("docker", "ecs", "eks", "openshift"),
    "containerization": ("docker", "podman", "container"),
    "aws": ("cloud", "amazon web services", "gcp", "azure"),
    "ci/cd": ("jenkins", "github actions", "gitlab", "deployment", "build automation"),
    "terraform": ("infrastructure as code", "cloudformation", "pulumi"),
    "postgresql": ("sql", "mysql", "database"),
    "mysql": ("sql", "postgresql", "database"),
    "react": ("javascript", "frontend", "vue", "angular"),
    "spark": ( "hadoop", "big data", "data engineering"),
    "pytorch": ("tensorflow", "deep learning", "machine learning"),
    "tensorflow": ("pytorch", "deep learning", "machine learning"),
    "fastapi": ("flask", "django", "rest", "api"),
    "docker": ("container", "podman", "kubernetes"),
    "golang": ("go", "backend"),
    "node.js": ("node", "javascript", "backend"),
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9+#./\s]", " ", text.lower()).strip()


def _tokens(text: str) -> set[str]:
    toks = {t for t in _norm(text).split() if len(t) > 1}
    # naive singular/plural stem so that "containers" matches "container"
    return {t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks}


def _met_status(req: str, skills: set[str]) -> str:
    req_n = _norm(req)
    if req_n in {_norm(s) for s in skills}:
        return "Met"
    req_t = _tokens(req)
    for skill in skills:
        if req_t & _tokens(skill):
            return "Partial"
    # transferable synonyms count as Partial, not Missing (more useful to the UI)
    for transferable in TRANSFER_MAP.get(req_n, ()):  # type: ignore[union-attr]
        if _norm(transferable) in {_norm(s) for s in skills}:
            return "Partial"
        if any(_norm(transferable) in _norm(s) for s in skills):
            return "Partial"
    return "Missing"


def _derive_gaps(jd: JobRequirements, profile: dict) -> list[dict]:
    skills = set(profile.get("skills", [])) | set(profile.get("tools", []))
    gaps = []
    for group in ("requiredSkills", "preferredSkills"):
        for req in getattr(jd, group):
            status = _met_status(req, skills)
            if status != "Met":
                gaps.append({
                    "requirement": req, "status": status,
                    "severity": _severity(req, jd),
                    "transferable": _transferable(req, skills),
                })
    return gaps


def _severity(req: str, jd: "JobRequirements") -> str:
    if req in jd.mustHaves:
        return "High"
    if req in jd.requiredSkills:
        return "Medium"
    return "Low"


def _transferable(req: str, skills: set[str]) -> str | None:
    """Nearest transferable skill the candidate HAS, for a missing requirement."""
    req_n = _norm(req)
    req_tokens = _tokens(req)
    # 1. curated adjacency map wins — those are deliberate near-synonyms
    for candidate_key, transferables in TRANSFER_MAP.items():
        if candidate_key and (candidate_key in req_n or req_n in candidate_key):
            for transferable in transferables:
                if any(transferable in _norm(s) for s in skills):
                    return next(s for s in skills if transferable in _norm(s))
    # 2. token overlap fallback
    best, best_overlap = None, 0
    for skill in skills:
        overlap = len(req_tokens & _tokens(skill))
        if overlap > best_overlap:
            best, best_overlap = skill, overlap
    return best if best_overlap else None


def _missing(req: str, profile: dict) -> bool:
    skills = set(profile.get("skills", [])) | set(profile.get("tools", []))
    return _met_status(req, skills) != "Met"


# ---------- claims / inconsistencies / questions (FR-08, FR-08b, FR-11) -------------

def _wrap(reply: object, key: str) -> dict:
    """Normalize a model reply to the schema shape: {key: [list of items]}.

    Handles the two reply shapes models actually emit that differ from the
    requested schema: a top-level JSON array, and a wrapper object with a
    differently-named key (e.g. "claims" instead of "claimChecks"). The first
    list value found wins; a valid reply is never lost to a shape mismatch.
    """
    if isinstance(reply, list):
        return {key: reply}
    if not isinstance(reply, dict):
        return {key: []}
    if key in reply:
        return reply
    for alt, val in reply.items():
        if isinstance(val, list):
            merged = dict(reply)
            del merged[alt]
            merged[key] = val
            return merged
    return {key: []}


def _check_claims(settings: object, resume_text: str, claims: list[str], title: str) -> dict:
    from src.shared.ai_factory import AIFactory

    if not claims:
        return {"claimChecks": []}
    template, _ = load_prompt("check_claims", "v1")
    user = render(template, resume_text=wrap_untrusted("RESUME", redact_pii(resume_text[:15000])),
                  claims_json=json.dumps(claims))
    bedrock = AIFactory.for_settings(settings)
    return ClaimsOut.model_validate(_wrap(bedrock.converse_json(
        system=INJECTION_PREAMBLE, user=user,
        model_id=settings.bedrock_model_judge), "claimChecks")).model_dump()


def _detect_inconsistencies(settings: object, resume_text: str, claims: dict) -> list[dict]:
    from src.shared.ai_factory import AIFactory

    template, _ = load_prompt("detect_inconsistencies", "v1")
    user = render(template, resume_text=wrap_untrusted("RESUME", redact_pii(resume_text[:15000])))
    bedrock = AIFactory.for_settings(settings)
    wrapped = _wrap(bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                          model_id=settings.bedrock_model_judge), "flags")
    # The model emits free-text flag types ("unexplained gap", "date overlap")
    # while the schema allows a fixed literal set — snap each to the nearest
    # allowed literal instead of losing the whole reply to a validation error.
    allowed = {"gap", "overlap", "implausible", "seniority", "claim", "inconsistency", "mustHave"}
    hints = (("gap", "gap"), ("overlap", "overlap"), ("implausib", "implausible"),
             ("senior", "seniority"), ("claim", "claim"))
    for flag in wrapped.get("flags", []):
        if isinstance(flag, dict):
            ftype = str(flag.get("type", "")).strip()
            low = ftype.lower()
            if ftype not in allowed:
                mapped = next((target for needle, target in hints if needle in low), "inconsistency")
                flag["type"] = mapped
    result = InconsistenciesOut.model_validate(wrapped)
    return [f.model_dump() for f in result.flags]


def _screening_questions(settings: object, jd: JobRequirements, gaps: list[dict],
                         claims: dict, flags: list[dict]) -> dict:
    from src.shared.ai_factory import AIFactory

    template, _ = load_prompt("screening_questions", "v1")
    analysis_brief = json.dumps({"gaps": gaps, "claimChecks": claims.get("claimChecks", []),
                                 "flags": flags}, ensure_ascii=False)
    user = render(template, analysis_json=analysis_brief,
                  job_json=json.dumps(jd.model_dump(), ensure_ascii=False))
    bedrock = AIFactory.for_settings(settings)
    data = _wrap(bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                       model_id=settings.bedrock_model_judge), "questions")
    return QuestionsOut.model_validate(data).model_dump()
