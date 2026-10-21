"""Workflow step 3 (FR-03, FR-05): extraction + embeddings + role suggestions.

1. Bedrock profile extraction validated against the extract_profile schema.
2. Terms normalised against the prototype's taxonomy lists (data/taxonomy/*.json).
3. Resume chunked + Titan-embedded into the vector store (owner-scoped).
4. Top-k role suggestions from the 32 seed roles (embedding cosine + rationale).
5. Candidate marked READY.
"""
from __future__ import annotations

import json
import math

import boto3

from src.shared.guards import INJECTION_PREAMBLE, wrap_untrusted
from src.shared.logging import get_logger
from src.shared.models import Profile
from src.shared.prompt_loader import load_prompt, record_version, render
from src.shared.repo import Repo
from src.shared.settings import load_settings
from src.shared.vector_store import VectorStore

LOG = get_logger("extract-profile")
TOP_ROLES = 5


def handler(event: dict, context: object) -> dict:
    settings = load_settings()
    repo = Repo(settings.table_name, region=settings.aws_region)
    sub, cid = event["userSub"], event["candidateId"]
    try:
        return _do_extract(event, context, settings, repo, sub, cid)
    except Exception as exc:
        LOG.exception("extract_profile failed", extra={"extra_fields": {"candidateId": cid, "sub": sub}})
        try:
            repo.record_failure(sub, cid, str(exc))
        except Exception:
            pass
        raise


def _do_extract(event: dict, context: object, settings: Any, repo: Repo, sub: str, cid: str) -> dict:
    s3 = boto3.client("s3", region_name=settings.aws_region)
    # Step Functions puts the previous Lambda's result under result_path
    # ($.text -> Payload key); accept both the flat and wrapped shapes.
    text_out = event.get("text") or {}
    text_key = (text_out.get("Payload") or text_out).get("textKey") or event.get("textKey")
    resume_text = s3.get_object(Bucket=event["bucket"], Key=text_key)["Body"].read().decode(errors="replace")

    from src.shared.ai_factory import AIFactory

    bedrock = AIFactory.for_settings(settings)
    template, _schema = load_prompt("extract_profile", "v1")
    user = render(template, resume_text=wrap_untrusted("RESUME", resume_text[:15000]))
    data = bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                 model_id=settings.bedrock_model_small)
    profile = Profile.model_validate(_strip(block=data))
    profile = _normalise_terms(profile)

    n_chunks = VectorStore(settings).upsert(sub, cid, resume_text)
    roles = _suggest_roles(bedrock, profile, settings)

    # Seed the GSI2 dedup index now that SHA-256 is known (validate step output).
    # create_candidate runs before the hash exists, so without this seeding the
    # duplicate check can never fire for anything (the index stays empty).
    validate_out = event.get("validate") or {}
    sha256 = (validate_out.get("Payload") or validate_out).get("sha256")
    if sha256:
        from src.shared.keys import cand_sk, gsi_dedup_pk

        repo.update_candidate(sub, cid, {
            "gsi2pk": gsi_dedup_pk(sub, sha256), "gsi2sk": cand_sk(cid),
        })

    repo.update_candidate(sub, cid, {
        "profile": profile.model_dump(),
        "status": "READY",
        "chunkCount": n_chunks,
        **record_version("extract_profile", "v1"),
    })
    repo.put_role_suggestion(sub, cid, roles)
    return {"candidateId": cid, "ready": True, "chunks": n_chunks, "roles": roles}


def _strip(block: dict) -> dict:
    """Bedrock envelope -> profile body (the model returns the schema flat)."""
    return block


def _normalise_terms(profile: Profile) -> Profile:
    lower_list = _taxonomy_lower()
    for field in ("skills", "tools", "languages"):
        normalised = {lower_list.get(t.lower().strip(), t.strip()) for t in getattr(profile, field)}
        setattr(profile, field, sorted(normalised))
    return profile


def _taxonomy_lower() -> dict[str, str]:
    from pathlib import Path

    mapping: dict[str, str] = {}
    base = Path(__file__).resolve().parents[2] / "data" / "taxonomy"
    for name in ("languages.json", "skills.json", "technologies.json"):
        path = base / name
        if path.exists():
            for term in json.loads(path.read_text(encoding="utf-8")):
                key = term if isinstance(term, str) else term.get("name", "")
                if key:
                    mapping[key.lower()] = key
    return mapping


def _suggest_roles(bedrock: object, profile: Profile, settings: object) -> list[dict]:
    """Embed candidate summary vs 32 seed roles; top-5 cosine + rationale."""
    roles_path = _roles_path()
    roles = json.loads(roles_path.read_text(encoding="utf-8"))
    candidate_text = ", ".join(
        [*profile.skills, *profile.tools, *profile.languages]
    ) + " " + (profile.summary or "")
    cand_vec, role_vecs = _embed_pair(bedrock, candidate_text,
                                      [r.get("description", "") for r in roles])
    scored = sorted(zip(role_vecs, roles), key=lambda p: -_cos(cand_vec, p[0]))[:TOP_ROLES]
    out = []
    for role_vec, role in scored:
        score = round(max(0.0, _cos(cand_vec, role_vec)) * 100)
        rationale = _rationale(bedrock, role, profile, settings)
        out.append({
            # Seed file keys are role/description (older drafts used id/title).
            "roleId": role.get("roleId") or role.get("id") or role.get("role", ""),
            "role": role.get("role") or role.get("title", ""),
            "score": score,
            # Deterministic fallback so the UI never shows an empty card
            # when the rationale model call fails.
            "rationale": rationale or (role.get("description") or "")[:400],
        })
    return out


def _rationale(bedrock: object, role: dict, profile: Profile, settings: object) -> str:
    try:
        template, _ = load_prompt("role_rationale", "v1")
        evidence = json.dumps({
            "skills": profile.skills[:20], "tools": profile.tools[:10],
            "languages": profile.languages[:10],
            "experience": [e.model_dump() for e in profile.experience[:5]],
        })
        user = render(template, role_title=role.get("title", ""),
                      role_description=role.get("description", ""),
                      profile_summary=wrap_untrusted("PROFILE_EVIDENCE", evidence))
        resp = bedrock.converse_json(system=INJECTION_PREAMBLE, user=user,
                                     model_id=settings.bedrock_model_small)
        return resp.get("rationale", "")[:400]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("role rationale failed", extra={"extra_fields": {"error": type(exc).__name__}})
        return ""


def _embed_pair(bedrock: object, a: str, bs: list[str]) -> tuple[list[float], list[list[float]]]:
    vecs = bedrock.embed([a] + bs)
    return vecs[0], vecs[1:]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-9
    nb = math.sqrt(sum(x * x for x in b)) or 1e-9
    return dot / (na * nb)


def _roles_path() -> object:
    from pathlib import Path

    # parents: [0]=pipeline/ [1]=functions/ [2]=src/ [3]=repo root, where the
    # CDK bundle places data/ (bundled from repo root next to src/ and prompts/).
    path = Path(__file__).resolve().parents[3] / "data" / "job_roles.json"
    if not path.exists():  # fallback for local layouts without the root copy
        path = Path(__file__).resolve().parents[2] / "data" / "job_roles.json"
    return path
