"""Route implementations for search (FR-12) and comparison (FR-10)."""
from __future__ import annotations

from src.shared.guards import INJECTION_PREAMBLE
from src.shared.keys import score_band
from src.shared.models import ParseSearchOut
from src.shared.prompt_loader import load_prompt, render
from src.shared.settings import Settings


def _years_of(profile: dict) -> float | None:
    """Approximate dated experience; None when the profile has no ratified dates."""
    years = []
    for exp in profile.get("experience", []):
        start = str(exp.get("start") or "")
        end = str(exp.get("end") or "2026")
        try:
            years.append(int(end[-4:]) - int(start[-4:]))
        except (ValueError, IndexError):
            continue
    if not years:
        return None
    return max(0, sum(max(0, y) for y in years))


def search(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """FR-12: structured filters + semantic query; results show why each matched."""
    from src.shared.ai_factory import AIFactory
    from src.shared.vector_store import VectorStore

    q = (body.get("query") or "").strip()
    if not q:
        from .handler import api_error

        raise api_error(400, "VALIDATION", "query is required")
    template, _ = load_prompt("parse_search_query", "v1")
    bedrock = AIFactory.for_settings(settings)
    filters = ParseSearchOut.model_validate(bedrock.converse_json(
        system=INJECTION_PREAMBLE,
        user=render(template, query=q),
        model_id=settings.bedrock_model_small,
    ))

    candidates, _cursor = repo.list_candidates(sub, status="READY", limit=200)
    if not candidates:
        return {"results": [], "filters": filters.model_dump()}

    # semantic: top hits per user, then per-candidate aggregation
    per_candidate_top: dict[str, float] = {}
    try:
        for hit in VectorStore(settings).query(sub, q, top_k=40):
            cid_val = hit.get("candidateId")
            if cid_val:
                per_candidate_top[cid_val] = max(per_candidate_top.get(cid_val, 0.0),
                                                 float(hit.get("score", 0.0)))
    except Exception:
        per_candidate_top = {}

    skills_l = {s.lower() for s in filters.skills}
    kw_l = {k.lower() for k in filters.keywords}
    domain_l = (filters.domain or "").lower()

    results = []
    for cand in candidates:
        profile = cand.get("profile", {})
        all_skills = {s.lower() for s in
                      (profile.get("skills", []) + profile.get("tools", [])
                       + profile.get("languages", []))}
        matched = sorted(skills_l & all_skills)
        why = []
        if matched:
            why.append("skills matched: " + ", ".join(matched))
        years = _years_of(profile)
        if filters.minYears is not None and years is not None:
            if years >= filters.minYears:
                why.append(f"{years:g}+ years of dated experience")
            else:
                continue  # hard filter
        if filters.seniority and profile.get("seniority"):  # optional extra filter
            if filters.seniority.lower() not in profile.get("seniority", "").lower():
                continue
        if kw_l:
            found = [k for k in kw_l if any(k in s or s in k for s in all_skills)]
            if found:
                why.append("keyword match: " + ", ".join(found))
        if domain_l and domain_l in (profile.get("summary", "") or "").lower():
            why.append(f"domain evidence: {filters.domain}")
        semantic = per_candidate_top.get(cand["candidateId"], 0.0)
        if filters.semanticQuery and semantic:
            why.append(f"semantic similarity {semantic:.2f}")
        if not why and not semantic:
            continue
        results.append({
            "candidateId": cand["candidateId"],
            "name": profile.get("name"),
            "matchedSkills": matched,
            "semanticScore": round(semantic, 3),
            "why": why,
        })
    results.sort(key=lambda r: (0 if r["matchedSkills"] else 1,
                                -r["semanticScore"]))
    return {"query": q, "filters": filters.model_dump(),
            "results": results[:10], "candidatesScanned": len(candidates)}


def compare(sub, body, params, query, repo, settings: Settings, **kw) -> dict:
    """FR-10: 2-5 candidates side by side; summary cites only stored analyses."""
    from src.shared.ai_factory import AIFactory
    from src.shared.models import CompareOut

    jid = params.get("jobId") or body.get("jobId")
    candidate_ids = list((body.get("candidateIds") or []))[:5]
    if len(candidate_ids) < 2:
        from .handler import api_error

        raise api_error(400, "VALIDATION", "compare needs 2-5 candidates")
    analyses = []
    names: dict[str, str] = {}
    for cid in candidate_ids:
        a = repo.get_analysis(sub, jid, cid)
        if not a:
            from .handler import not_found

            raise not_found()
        cand = repo.get_candidate(sub, cid) or {}
        names[cid] = ((cand.get("profile") or {}).get("name")
                      or cand.get("filename") or cid)
        analyses.append({
            "candidateId": cid,
            "totalScore": a.get("totalScore"),
            "criteria": a.get("criteria", []),
            "gaps": a.get("gaps", []),
            "claimChecks": a.get("claimChecks", []),
            "flags": a.get("flags", []),
        })
    bedrock = AIFactory.for_settings(settings)
    template, _ = load_prompt("compare_candidates", "v1")
    user = render(template,
                  job_json='{"jobId": "%s"}' % jid,
                  analyses_json=_safe_json(analyses))
    data = bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                 model_id=settings.bedrock_model_judge,
                                 temperature=0.2)
    comparison = CompareOut.model_validate(data)
    return {
        "jobId": jid,
        "candidateIds": candidate_ids,
        "names": names,
        "summary": comparison.summary,
        "strengths": comparison.strengths,
        "matrix": [{
            "candidateId": a["candidateId"],
            "name": names.get(a["candidateId"], ""),
            "totalScore": a["totalScore"],
            "band": score_band(int(a["totalScore"] or 0)),
            "criteria": {c["name"]: c["score"] for c in a["criteria"]},
        } for a in analyses],
    }


def _safe_json(obj) -> str:
    import json

    return json.dumps(obj, default=str)
