"""OCR fallback for scanned documents.

Only runs when the text layer is too sparse to be real. Confidence is captured
and carried forward: a low-confidence extraction is flagged for manual review
and penalised in ranking rather than silently treated as fact.
"""

from __future__ import annotations

import structlog

from screener_api.parse.extract import Extraction, ExtractionError, normalise

log = structlog.get_logger()

MIN_CONFIDENCE = 60
OCR_DPI = 200  # 300 is sharper and roughly 2x slower; 200 reads resumes fine
OCR_MAX_PAGES = 10  # OCR is the slowest thing in the pipeline; cap the blast radius


def ocr_pdf(data: bytes, *, dpi: int = OCR_DPI, max_pages: int = OCR_MAX_PAGES) -> Extraction:
    import pytesseract
    from pdf2image import convert_from_bytes

    try:
        images = convert_from_bytes(data, dpi=dpi, first_page=1, last_page=max_pages)
    except Exception as exc:
        raise ExtractionError(f"rasterisation failed: {type(exc).__name__}") from exc

    if not images:
        raise ExtractionError("no pages to rasterise")

    texts: list[str] = []
    confidences: list[int] = []
    for image in images:
        try:
            data_frame = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        except Exception as exc:
            log.warning("ocr.page_failed", error=type(exc).__name__)
            continue
        words = data_frame.get("text", [])
        confs = [int(c) for c in data_frame.get("conf", []) if str(c).lstrip("-").isdigit()]
        texts.append(" ".join(w for w in words if w.strip()))
        # -1 marks a region with no text; averaging it in would understate quality.
        confidences.extend(c for c in confs if c >= 0)

    text = normalise("\n".join(texts))
    confidence = int(sum(confidences) / len(confidences)) if confidences else 0

    return Extraction(
        text=text,
        extractor="tesseract",
        page_count=len(images),
        chars_per_page=len(text) / len(images) if images else 0.0,
        ocr_used=True,
        ocr_confidence=confidence,
    )


def is_low_confidence(extraction: Extraction) -> bool:
    return (
        extraction.ocr_used
        and extraction.ocr_confidence is not None
        and extraction.ocr_confidence < MIN_CONFIDENCE
    )
