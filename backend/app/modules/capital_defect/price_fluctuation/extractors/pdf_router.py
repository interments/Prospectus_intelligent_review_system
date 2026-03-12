from __future__ import annotations

from pathlib import Path

from ..schemas.models import TableBlock, TextBlock
from .plumber_extractor import PlumberExtractor


class PdfRouter:
    """Unified extraction entry. OCR backend can be added later; pdfplumber fallback now."""

    def __init__(self) -> None:
        self.plumber = PlumberExtractor()

    def extract(self, pdf_path: str | Path) -> tuple[list[TextBlock], list[TableBlock]]:
        return self.plumber.extract(pdf_path)
