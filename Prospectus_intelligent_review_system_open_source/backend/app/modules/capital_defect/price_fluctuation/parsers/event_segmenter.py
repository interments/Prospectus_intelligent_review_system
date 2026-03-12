from __future__ import annotations

import re

from ..schemas.models import SectionChunk, TableBlock, TextBlock

SUBSUB_TITLE_RE = re.compile(
    r"(^|\n)\s*([（(]?\d+[）)]|\d+\.|[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十]+\))\s*([^\n]{0,50})"
)


class EventSegmenter:
    def segment(self, candidate_pages: list[TextBlock], tables: list[TableBlock]) -> list[SectionChunk]:
        chunks: list[SectionChunk] = []
        table_by_page: dict[int, list[TableBlock]] = {}
        for t in tables:
            table_by_page.setdefault(t.page, []).append(t)

        for block in candidate_pages:
            text = block.text
            matches = list(SUBSUB_TITLE_RE.finditer(text))
            if not matches:
                # one chunk per page fallback
                content = text + self._table_text(table_by_page.get(block.page, []))
                chunks.append(
                    SectionChunk(
                        title=f"page_{block.page}",
                        content=content,
                        page_start=block.page,
                        page_end=block.page,
                    )
                )
                continue

            for i, m in enumerate(matches):
                start = m.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                title = m.group(0).strip().replace("\n", " ")[:80]
                content = text[start:end].strip() + self._table_text(table_by_page.get(block.page, []))
                chunks.append(
                    SectionChunk(
                        title=title,
                        content=content,
                        page_start=block.page,
                        page_end=block.page,
                    )
                )
        return chunks

    @staticmethod
    def _table_text(page_tables: list[TableBlock]) -> str:
        if not page_tables:
            return ""
        lines: list[str] = ["\n\n[PAGE_TABLES]"]
        for tb in page_tables:
            lines.append(f"\n[TABLE #{tb.table_index}]")
            for row in tb.rows:
                lines.append(" | ".join([c if c is not None else "" for c in row]))
        return "\n".join(lines)
