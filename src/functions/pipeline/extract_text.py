"""Workflow step 2: extract ordered text.

Default path (Free-plan, doc deviation section 2 of the plan):
  - PDF: PyMuPDF text-layer extraction page by page; pages with too little text
    (<120 chars) are rasterized and read by the Nova Lite vision model.
  - DOCX: docx2txt in Lambda.
Dormant path (behind SSM flag TEXT_SOURCE=textract, coded and moto-tested, not
billed while unset): Amazon Textract StartDocumentAnalysis + SNS completion.

Writes text/{candidateId}.txt to the uploads bucket and returns the text key.
"""
from __future__ import annotations

import boto3

from src.shared.logging import get_logger
from src.shared.repo import Repo
from src.shared.settings import load_settings

LOG = get_logger("extract-text")
LOW_TEXT_CHARS = 120
VISION_OCR_PROMPT = (
    "OCR this resume page. Return ONLY the page text in original reading order, "
    "no commentary, no markdown."
)


def handler(event: dict, context: object) -> dict:
    settings = load_settings()
    s3 = boto3.client("s3", region_name=settings.aws_region)
    sub, cid = event["userSub"], event["candidateId"]
    body = s3.get_object(Bucket=event["bucket"], Key=event["s3Key"])["Body"].read()

    if body[:5] == b"%PDF-":
        text = extract_pdf(settings, body)
    elif body[:4] == b"PK\x03\x04":
        import io

        import docx2txt

        text = docx2txt.process(io.BytesIO(body))
    else:
        text = ""

    text = "\n".join(line for line in text.splitlines() if line.strip()).strip()
    if len(text) < 50:
        _repo().record_failure(sub, cid, "Could not extract any text from the file")
        return {"extracted": False, "status": "FAILED",
                "failureReason": "Could not extract any text from the file"}

    text_key = f"text/{cid}.txt"
    s3.put_object(Bucket=event["bucket"], Key=text_key,
                  Body=text.encode(), ServerSideEncryption="AES256")
    return {"extracted": True, "textKey": text_key, "charCount": len(text)}


def extract_pdf(settings: object, body: bytes) -> str:
    """PyMuPDF per page; vision fallback on low-text pages."""
    import fitz  # PyMuPDF

    pages: list[str] = []
    with fitz.open(stream=body, filetype="pdf") as doc:
        for page in doc:
            page_text = page.get_text("text", sort=True)
            if len(page_text.strip()) < LOW_TEXT_CHARS:
                page_text = _vision_ocr(settings, page) or page_text
            pages.append(page_text)
    return "\n\n".join(pages)


def _vision_ocr(settings: object, page: object) -> str | None:
    try:
        from src.shared.ai_factory import AIFactory

        pix = page.get_pixmap(dpi=150)  # type: ignore[union-attr]
        png_bytes = pix.tobytes("png")
        client = AIFactory.for_settings(settings)
        return client.converse_vision(VISION_OCR_PROMPT, png_bytes).strip()
    except Exception as exc:  # noqa: BLE001 - degrade to text layer only
        LOG.warning("vision OCR failed", extra={"extra_fields": {"error": type(exc).__name__}})
        return None


def _textract_start_and_wait(event: dict, s3: object) -> str:  # pragma: no cover
    """Dormant Textract path (enabled only when TEXT_SOURCE=textract)."""
    textract = boto3.client("textract", region_name=_settings().aws_region)
    job = textract.start_document_analysis(
        DocumentLocation={
            "S3Object": {"Bucket": event["bucket"], "Name": event["s3Key"]}},
        FeatureTypes=["LAYOUT"],
    )
    waiter = textract.get_waiter("document_analysis_complete")
    waiter.wait(JobId=job["JobId"])
    pages, next_token = [], None
    while True:
        kwargs = {"JobId": job["JobId"]}
        if next_token:
            kwargs["NextToken"] = next_token
        resp = textract.get_document_analysis(**kwargs)
        pages += _blocks_to_text(resp)
        next_token = resp.get("NextToken")
        if not next_token:
            break
    return "\n\n".join(pages)


def _blocks_to_text(resp: dict) -> list[str]:  # pragma: no cover
    lines: dict[str, str] = {}
    for block in resp.get("Blocks", []):
        if block.get("BlockType") == "LINE":
            key = f"p{block.get('Page', 1)}:{block.get('RowIdx', block.get('Id', ''))}"
            lines.setdefault(key, block.get("Text", ""))
    return list(lines.values())


def _settings() -> object:
    return load_settings()


def _repo() -> Repo:
    settings = load_settings()
    return Repo(settings.table_name, region=settings.aws_region)
