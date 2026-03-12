from __future__ import annotations

from pathlib import Path

import pdfplumber

from ..schemas.models import TableBlock, TextBlock


class PlumberExtractor:
    def extract(self, pdf_path: str | Path) -> tuple[list[TextBlock], list[TableBlock]]:
        text_blocks: list[TextBlock] = []
        table_blocks: list[TableBlock] = []

        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                if text.strip():
                    text_blocks.append(TextBlock(page=idx, text=text))

                try:
                    tables = page.extract_tables() or []
                except Exception:
                    tables = []
                for t_idx, rows in enumerate(tables):
                    norm_rows: list[list[str | None]] = []
                    for r in rows:
                        norm_rows.append([None if c is None else str(c).strip() for c in r])
                    table_blocks.append(TableBlock(page=idx, table_index=t_idx, rows=norm_rows))

        return text_blocks, table_blocks
