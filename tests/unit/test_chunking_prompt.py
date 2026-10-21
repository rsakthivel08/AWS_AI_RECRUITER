"""Chunking (RAG ingestion parity) and prompt-loader tests."""
from src.shared.chunking import chunk_text
from src.shared.prompt_loader import load_prompt, render


def test_chunk_text_returns_500_word_chunks_with_overlap():
    text = " ".join(f"word{i}" for i in range(1200))
    chunks = chunk_text(text)
    assert len(chunks) == 3                      # 1200 words / step 450
    assert len(chunks[0].split()) == 500
    # overlap: the last 50 words of chunk0 reappear at the head of chunk1
    tail = chunks[0].split()[-50:]
    head = chunks[1].split()[:50]
    assert tail == head
    assert chunks[1].split()[0] == "word450"


def test_chunk_text_short_text_single_chunk():
    text = "short resume text"
    assert chunk_text(text) == [text]


def test_chunk_text_empty():
    assert chunk_text("") == []


def test_prompts_load_with_schemas():
    names = ["extract_profile", "parse_jd", "judge_criterion", "check_claims",
             "detect_inconsistencies", "screening_questions", "parse_search_query",
             "answer_about_candidate", "compare_candidates", "role_rationale"]
    for name in names:
        template, schema = load_prompt(name, "v1")
        assert template.strip(), f"{name} template empty"
        assert "type" in schema and schema.get("type") == "object", f"{name} schema invalid"


def test_render_fills_placeholders():
    template, _ = load_prompt("check_claims", "v1")
    out = render(template, resume_text="ABC", claims_json='["c1"]')
    assert "ABC" in out and '"c1"' in out
    assert "{claims_json}" not in out
