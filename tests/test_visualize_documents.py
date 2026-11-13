"""Importing tables from PDF, Word and pictures (OCR) through the sandboxed visualization engine."""

from __future__ import annotations

import base64
import csv
import io
from pathlib import Path

import pytest

from apps.analytics.sandbox import run_engine
from apps.analytics.services import AnalysisError
from apps.visualize.services import VIZ_ENGINE

ROWS = [
    ["Region", "Month", "Sales"],
    ["East", "Jan", "1,200"],
    ["West", "Jan", "980"],
    ["North", "Feb", "1,450"],
    ["South", "Feb", "760"],
]


def _imported(content: bytes, fmt: str) -> list[dict[str, str]]:
    output = run_engine("viz_import", content, script=VIZ_ENGINE, format=fmt)
    return list(csv.DictReader(io.StringIO(base64.b64decode(output["csv"]).decode("utf-8"))))


def _font() -> str:
    import matplotlib

    return str(Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans.ttf")


def _table_image() -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(_font(), 30)
    image = Image.new("RGB", (900, 420), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 20), "Quarterly sales", font=font, fill="black")
    for r, row in enumerate(ROWS):
        y = 90 + r * 62
        draw.line((30, y - 10, 860, y - 10), fill="gray", width=2)
        for c, cell in enumerate(row):
            draw.text((50 + c * 280, y), cell, font=font, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _table_docx() -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph("Quarterly sales")
    table = document.add_table(rows=len(ROWS), cols=3)
    for r, row in enumerate(ROWS):
        for c, cell in enumerate(row):
            table.cell(r, c).text = cell
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _table_pdf() -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    figure, axis = plt.subplots(figsize=(6, 3))
    axis.axis("off")
    axis.table(cellText=ROWS[1:], colLabels=ROWS[0], loc="center")
    output = io.BytesIO()
    figure.savefig(output, format="pdf")
    plt.close(figure)
    return output.getvalue()


def _check(rows: list[dict[str, str]]) -> None:
    assert [row["Region"] for row in rows] == ["East", "West", "North", "South"]
    assert [row["Month"] for row in rows] == ["Jan", "Jan", "Feb", "Feb"]
    # Thousands separators are understood: the column is numeric.
    assert [float(row["Sales"]) for row in rows] == [1200, 980, 1450, 760]


def test_word_table_is_imported_with_its_header():
    _check(_imported(_table_docx(), "docx"))


def test_pdf_table_is_imported():
    _check(_imported(_table_pdf(), "pdf"))


def test_picture_of_a_table_is_read_by_ocr_into_rows_and_columns():
    _check(_imported(_table_image(), "png"))


def test_document_without_a_table_explains_what_to_do():
    import docx

    document = docx.Document()
    document.add_paragraph("Only text here")
    output = io.BytesIO()
    document.save(output)
    with pytest.raises(AnalysisError, match="no table"):
        _imported(output.getvalue(), "docx")
