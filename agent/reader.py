"""Get text out of a PDF: the embedded text layer if there is one, otherwise OCR."""
import io
from functools import lru_cache

import pdfplumber

MIN_TEXT_CHARS = 40  # fewer than this on page 1 = treat as a scanned image


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr_onnxruntime import RapidOCR   # offline ONNX model, no API key
    return RapidOCR()


def read_text_layer(pdf_bytes):
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = len(pdf.pages)
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    return pages, text


def ocr(pdf_bytes):
    """OCR every page. Returns (text, mean confidence, min line confidence)."""
    import numpy as np
    import pypdfium2 as pdfium
    engine = _ocr_engine()
    doc = pdfium.PdfDocument(pdf_bytes)
    lines, scores = [], []
    for page in doc:
        img = page.render(scale=2).to_pil()
        result, _ = engine(np.array(img))
        lines += _rows_to_lines(result or [], scores)
    mean = sum(scores) / len(scores) if scores else 0.0
    return "\n".join(lines), round(mean, 3), round(min(scores), 3) if scores else 0.0


def _rows_to_lines(result, scores):
    """OCR returns word boxes; group boxes on the same baseline into text lines, left to right."""
    boxes = []
    for box, text, score in result:
        y = sum(p[1] for p in box) / 4
        h = abs(box[2][1] - box[0][1])
        boxes.append((y, h, box[0][0], text, float(score)))
        scores.append(float(score))
    boxes.sort()
    lines, cur = [], []
    for b in boxes:
        if cur and abs(b[0] - cur[-1][0]) > max(8, 0.5 * b[1]):
            lines.append(cur)
            cur = []
        cur.append(b)
    if cur:
        lines.append(cur)
    return [" ".join(t for *_, t, _ in sorted(l, key=lambda b: b[2])) for l in lines]


def render_preview(pdf_bytes, width=900):
    """First page as a PIL image, for the UI."""
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(pdf_bytes)[0]
    return page.render(scale=width / page.get_width()).to_pil()
