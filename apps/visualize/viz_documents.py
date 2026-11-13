"""Tables from documents and pictures, for the visualization engine's import (sandboxed).

- PDF: the text layer's tables (pdfplumber); scanned pages without text are rendered and read by OCR.
- Word (.docx): the document's tables (python-docx).
- Images (PNG, JPEG, WebP, BMP, TIFF): OCR (RapidOCR, ONNX models shipped with the package), then the
  words are put back into rows and columns from their positions, so the table keeps the layout of the
  picture: one text line per row, one column per vertical lane of text.

Every function returns rows of cell strings, header first. Errors are DocumentError (shown to the user).
"""

from __future__ import annotations

import io
import re
import statistics
from typing import Any

IMAGE_FORMATS = {"png", "jpg", "jpeg", "webp", "bmp", "tif", "tiff", "gif"}
MAX_PAGES = 30
MAX_IMAGE_PIXELS = 40_000_000

Rows = list[list[str]]


class DocumentError(ValueError):
    """The document holds no readable table (the message is shown to the user)."""


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()


def _square(rows: Rows) -> Rows:
    """Drops empty rows/columns and pads rows to one width."""
    rows = [[_clean(cell) for cell in row] for row in rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    keep = [i for i in range(width) if any(row[i] for row in rows)]
    return [[row[i] for i in keep] for row in rows]


def _merge_tables(tables: list[Rows]) -> Rows:
    """The main table: the largest, with continuation tables (same header or same width) appended."""
    tables = [table for table in (_square(t) for t in tables) if len(table) >= 2 and len(table[0]) >= 2]
    if not tables:
        raise DocumentError("No table was found in this file.")
    main = max(tables, key=lambda t: len(t) * len(t[0]))
    header = main[0]
    merged = list(main)
    for table in tables:
        if table is main or len(table[0]) != len(header):
            continue
        body = table[1:] if table[0] == header else table
        merged.extend(body)
    return merged


# --- PDF ---------------------------------------------------------------------------------------


def pdf_rows(content: bytes) -> Rows:
    import pdfplumber

    tables: list[Rows] = []
    scanned: list[Any] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages[:MAX_PAGES]:
                found = page.extract_tables() or page.extract_tables(
                    {"vertical_strategy": "text", "horizontal_strategy": "text"}
                )
                tables.extend([[list(row) for row in table] for table in found if table])
                if not page.chars:
                    scanned.append(page.page_number)
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed PDFs raise many library errors
        raise DocumentError(f"The PDF could not be read ({type(exc).__name__}).") from exc
    if tables:
        return _merge_tables(tables)
    if scanned:
        return _merge_tables([image_rows(png) for png in _render_pdf_pages(content, scanned)])
    raise DocumentError("No table was found in this PDF.")


def _render_pdf_pages(content: bytes, page_numbers: list[int]) -> list[bytes]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    images = []
    for number in page_numbers[:MAX_PAGES]:
        bitmap = pdf[number - 1].render(scale=2.0)
        output = io.BytesIO()
        bitmap.to_pil().save(output, format="PNG")
        images.append(output.getvalue())
    return images


# --- Word --------------------------------------------------------------------------------------


def docx_rows(content: bytes) -> Rows:
    import docx

    if not content.startswith(b"PK"):
        raise DocumentError("This is not a .docx document (old .doc files: save them as .docx first).")
    try:
        document = docx.Document(io.BytesIO(content))
        tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in document.tables]
    except Exception as exc:  # noqa: BLE001 - malformed documents raise many library errors
        raise DocumentError(f"The Word document could not be read ({type(exc).__name__}).") from exc
    if not tables:
        raise DocumentError("This Word document has no table. Put the data in a table, then upload it again.")
    return _merge_tables(tables)


# --- Images (OCR) --------------------------------------------------------------------------------

_ENGINE: Any = None


def _ocr() -> Any:
    global _ENGINE
    if _ENGINE is None:
        import onnxruntime
        import rapidocr_onnxruntime.utils as ocr_utils
        from rapidocr_onnxruntime import RapidOCR

        def session_options() -> Any:
            # ONNX Runtime starts one thread per CPU core by default (it ignores OMP_NUM_THREADS); keep
            # the sandboxed child small and inside its memory limit.
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = 2
            options.inter_op_num_threads = 1
            return options

        ocr_utils.SessionOptions = session_options
        _ENGINE = RapidOCR()
    return _ENGINE


def _prepare_image(content: bytes) -> Any:
    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(content))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise DocumentError("The image is too large; crop it to the table first.")
        image = ImageOps.exif_transpose(image).convert("RGB")
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001 - unreadable images raise many library errors
        raise DocumentError("The image could not be opened.") from exc
    # Small screenshots read better enlarged.
    if max(image.size) < 1400:
        factor = 1400 / max(image.size)
        image = image.resize((int(image.width * factor), int(image.height * factor)))
    import numpy as np

    return np.asarray(image)


def image_rows(content: bytes) -> Rows:
    words = _read_words(content)
    if len(words) < 4:
        raise DocumentError(
            "No table could be read from this image. Use a sharper, straight picture of the table."
        )
    return _layout_table(words)


def _read_words(content: bytes) -> list[dict[str, Any]]:
    image = _prepare_image(content)
    try:
        result, _ = _ocr()(image)
    except Exception as exc:  # noqa: BLE001 - the OCR runtime raises its own error types
        raise DocumentError(
            "The picture could not be read. Try a smaller, sharper picture that shows only the table."
        ) from exc
    words = []
    for box, text, score in result or []:
        text = _clean(text)
        if not text or float(score) < 0.35:
            continue
        xs = [point[0] for point in box]
        ys = [point[1] for point in box]
        words.append({"text": text, "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys)})
    return words


def _layout_table(words: list[dict[str, Any]]) -> Rows:
    """Rows from text lines, columns from vertical lanes of text, as the table is laid out."""
    height = statistics.median(word["y1"] - word["y0"] for word in words)
    # Rows: words whose vertical centres are within half a line of each other.
    lines: list[list[dict[str, Any]]] = []
    for word in sorted(words, key=lambda w: (w["y0"] + w["y1"]) / 2):
        centre = (word["y0"] + word["y1"]) / 2
        if (
            lines
            and abs(centre - statistics.mean((w["y0"] + w["y1"]) / 2 for w in lines[-1])) <= height * 0.55
        ):
            lines[-1].append(word)
        else:
            lines.append([word])
    # Titles above the table (a single wide line) are not rows.
    widest = max(len(line) for line in lines)
    while len(lines) > 2 and len(lines[0]) == 1 and widest >= 2:
        lines.pop(0)
    # Columns: merge the horizontal extents of words across lines into lanes.
    spans = sorted((w["x0"], w["x1"]) for line in lines for w in line)
    lanes: list[list[float]] = []
    gap = height * 0.6
    for x0, x1 in spans:
        if lanes and x0 <= lanes[-1][1] + gap:
            lanes[-1][1] = max(lanes[-1][1], x1)
        else:
            lanes.append([x0, x1])
    rows: Rows = []
    for line in lines:
        cells = [""] * len(lanes)
        for word in sorted(line, key=lambda w: w["x0"]):
            overlaps = [min(word["x1"], lane[1]) - max(word["x0"], lane[0]) for lane in lanes]
            index = max(range(len(lanes)), key=lambda i: overlaps[i])
            cells[index] = f"{cells[index]} {word['text']}".strip()
        rows.append(cells)
    table = _square(rows)
    if len(table) < 2 or len(table[0]) < 2:
        raise DocumentError(
            "No table could be read from this image. Use a sharper, straight picture of the table."
        )
    return table


def rows_to_frame(rows: Rows) -> Any:
    """A DataFrame with the first row as the header (made unique), numbers parsed where they are numbers."""
    import pandas as pd

    header, body = rows[0], rows[1:]
    if all(_looks_numeric(cell) for cell in header if cell):
        header, body = [f"Column {i + 1}" for i in range(len(header))], rows
    seen: dict[str, int] = {}
    names = []
    for i, name in enumerate(header):
        name = name or f"Column {i + 1}"
        seen[name] = seen.get(name, 0) + 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    frame = pd.DataFrame(body, columns=names)
    for column in frame.columns:
        values = frame[column].replace("", None)
        cleaned = values.dropna().map(_number_text)
        if len(cleaned) and cleaned.map(_looks_numeric).all():
            frame[column] = pd.to_numeric(
                values.map(lambda v: _number_text(v) if v is not None else None), errors="coerce"
            )
    return frame


def _number_text(value: Any) -> str:
    text = str(value).strip().replace(" ", "")
    text = re.sub(r"^[$€£¥₹]|[$€£¥₹%]$", "", text)
    if re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text):
        text = text.replace(",", "")
    if re.fullmatch(r"\(\d+(\.\d+)?\)", text):
        text = "-" + text[1:-1]
    return text


def _looks_numeric(value: Any) -> bool:
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", _number_text(value)))
