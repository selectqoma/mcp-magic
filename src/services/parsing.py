"""PDF solicitation parsing service.

Pipeline:
  1. Download PDF from URL
  2. Convert to Markdown via pymupdf4llm
  3. If text density is too low (likely a scan), fall back to pytesseract OCR
  4. Send Markdown to Claude Haiku with structured extraction prompt
  5. Return structured requirements dict + confidence score
"""

import ipaddress
import logging
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

# Minimum average characters per page — below this we assume scanned PDF
MIN_CHARS_PER_PAGE = 100

# Max PDF size to download (20 MB)
MAX_PDF_BYTES = 20 * 1024 * 1024

# Allowed hostname suffixes for PDF downloads — SSRF protection
ALLOWED_HOSTS = (
    ".gov",
    ".mil",
)


def _is_safe_url(url: str) -> bool:
    """Block private IPs and non-government hosts."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        # Block private/loopback IPs
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass  # not an IP — check hostname
        # Only allow .gov and .mil domains
        if not any(host.endswith(suffix) for suffix in ALLOWED_HOSTS):
            logger.warning(f"PDF URL blocked (non-gov host): {host}")
            return False
        return True
    except Exception:
        return False

EXTRACTION_PROMPT = """You are extracting structured information from a US government solicitation document.

<document>
{text}
</document>

Extract the following fields. If a field is not found, use null.
Return ONLY valid JSON, no explanation.

{{
  "requirements": ["list of key technical/programmatic requirements"],
  "evaluation_criteria": [
    {{"criterion": "name", "weight": "percentage or description if stated"}}
  ],
  "key_dates": [
    {{"event": "description", "date": "YYYY-MM-DD or text if no exact date"}}
  ],
  "page_limits": {{"description": "any page/volume limits mentioned"}},
  "eligible_entities": ["types of organizations eligible to respond"],
  "clearance_required": "security clearance level if mentioned, else null",
  "period_of_performance": "duration or date range if mentioned",
  "place_of_performance": "location if mentioned",
  "contract_type": "e.g. CPFF, FFP, T&M if mentioned"
}}"""


async def parse_solicitation_pdf(url: str) -> dict:
    """Download and parse a solicitation PDF. Returns structured data + confidence."""
    try:
        import pymupdf4llm
        import fitz  # PyMuPDF
    except ImportError:
        return {"error": "PDF parsing library not installed", "parsing_confidence": 0.0}

    # SSRF protection: only allow .gov/.mil hosts
    if not _is_safe_url(url):
        return {"error": "PDF URL not allowed (must be a .gov or .mil domain).", "parsing_confidence": 0.0}

    # Download the PDF with size limit
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_length = int(resp.headers.get("content-length", 0))
            if content_length > MAX_PDF_BYTES:
                return {"error": f"PDF too large ({content_length // 1024 // 1024}MB, max 20MB).", "parsing_confidence": 0.0}
            # Stream read with size cap
            data = b""
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                data += chunk
                if len(data) > MAX_PDF_BYTES:
                    return {"error": "PDF too large (>20MB).", "parsing_confidence": 0.0}
    except Exception as e:
        logger.error(f"Failed to download PDF from {url}: {e}")
        return {"error": f"Could not download PDF: {e}", "parsing_confidence": 0.0}

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        # Extract text via pymupdf4llm
        md_text = pymupdf4llm.to_markdown(str(tmp_path))

        # Check text density
        doc = fitz.open(str(tmp_path))
        num_pages = len(doc)
        doc.close()

        chars_per_page = len(md_text) / max(num_pages, 1)

        if chars_per_page < MIN_CHARS_PER_PAGE:
            logger.warning(f"Low text density ({chars_per_page:.0f} chars/page), trying OCR")
            md_text = _ocr_fallback(tmp_path)

    except Exception as e:
        logger.error(f"PDF text extraction failed: {e}")
        return {"error": f"PDF extraction failed: {e}", "parsing_confidence": 0.0}
    finally:
        tmp_path.unlink(missing_ok=True)

    if not md_text or len(md_text) < 100:
        return {"error": "Could not extract text from PDF", "parsing_confidence": 0.0}

    # Truncate to avoid huge token bills — most key info is in first ~50k chars
    md_text = md_text[:50000]

    return await _extract_with_haiku(md_text)


def _ocr_fallback(pdf_path: Path) -> str:
    """OCR fallback for scanned PDFs using pytesseract."""
    try:
        import fitz
        import pytesseract
        from PIL import Image
        import io

        doc = fitz.open(str(pdf_path))
        texts = []
        for page in doc:
            pix = page.get_pixmap(dpi=150)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            texts.append(pytesseract.image_to_string(img))
        doc.close()
        return "\n\n".join(texts)
    except Exception as e:
        logger.error(f"OCR fallback failed: {e}")
        return ""


async def _extract_with_haiku(text: str) -> dict:
    """Send text to Claude Haiku for structured extraction."""
    import json
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    try:
        message = await client.messages.create(
            model=settings.haiku_model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": EXTRACTION_PROMPT.format(text=text),
                }
            ],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        data = json.loads(raw)
        confidence = _compute_confidence(data)
        data["parsing_confidence"] = confidence
        return data

    except Exception as e:
        logger.error(f"Haiku extraction failed: {e}")
        return {"error": f"Extraction failed: {e}", "parsing_confidence": 0.0}


def _compute_confidence(data: dict) -> float:
    """Score confidence 0-1 based on how many key fields were found."""
    key_fields = [
        "requirements", "evaluation_criteria", "key_dates",
        "eligible_entities", "period_of_performance",
    ]
    found = sum(1 for f in key_fields if data.get(f))
    return round(found / len(key_fields), 2)
