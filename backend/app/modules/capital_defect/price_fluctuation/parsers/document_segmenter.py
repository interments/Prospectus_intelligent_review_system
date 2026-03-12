from __future__ import annotations

import re
from dataclasses import dataclass

from ..schemas.models import SectionChunk, TextBlock


@dataclass
class _Line:
    page: int
    text: str


class DocumentSegmenter:
    """Segment by full-document regex instead of page chunks."""

    main_re = re.compile(r"第[一二三四五六七八九十0-9]+[章节]\s*(?:发行人|公司)(?:的)?基本情况")
    chapter_re = re.compile(r"第[一二三四五六七八九十0-9]+[章节]")
    sub_re = re.compile(r"([一二三四五六七八九十0-9]+[、.．])?\s*.*?(股本|股权|股票).{0,8}(变化|变动)")
    mini_title_re = re.compile(r"^\s*([（(]?\d+[）)]|\d+[、.．]|[一二三四五六七八九十]+[、.．])\s*")

    def segment(self, text_blocks: list[TextBlock]) -> tuple[list[SectionChunk], dict]:
        lines = self._to_lines(text_blocks)
        debug = {
            "total_lines": len(lines),
            "main_start_line": None,
            "main_end_line": None,
            "sub_start_line": None,
            "sub_end_line": None,
        }

        main_start = self._find_line(lines, self.main_re)
        if main_start is None:
            return [], debug
        debug["main_start_line"] = main_start

        main_end = self._find_next_chapter(lines, main_start + 1)
        if main_end is None:
            main_end = len(lines)
        debug["main_end_line"] = main_end

        sub_start = self._find_line(lines[main_start:main_end], self.sub_re)
        if sub_start is None:
            return [], debug
        sub_start = main_start + sub_start
        debug["sub_start_line"] = sub_start

        # 该子标题常位于“发行人基本情况”章节早段，实务中后续仍有大量历次变动信息。
        # 为避免过早截断，这里直接延展到 main_end，再交给小标题切分。
        sub_end = main_end
        debug["sub_end_line"] = sub_end

        target = lines[sub_start:sub_end]
        return self._split_mini_sections(target), debug

    def _split_mini_sections(self, lines: list[_Line]) -> list[SectionChunk]:
        indices = []
        for i, ln in enumerate(lines):
            if self.mini_title_re.search(ln.text):
                indices.append(i)
        if not indices:
            if not lines:
                return []
            return [SectionChunk(title="target_section", content="\n".join([x.text for x in lines]), page_start=lines[0].page, page_end=lines[-1].page)]

        chunks: list[SectionChunk] = []
        for i, start in enumerate(indices):
            end = indices[i + 1] if i + 1 < len(indices) else len(lines)
            seg = lines[start:end]
            title = seg[0].text[:80]
            chunks.append(SectionChunk(title=title, content="\n".join([x.text for x in seg]), page_start=seg[0].page, page_end=seg[-1].page))
        return chunks

    @staticmethod
    def _to_lines(text_blocks: list[TextBlock]) -> list[_Line]:
        out: list[_Line] = []
        for b in text_blocks:
            for ln in b.text.splitlines():
                t = ln.strip()
                if t:
                    out.append(_Line(page=b.page, text=t))
        return out

    @staticmethod
    def _is_tocish_line(t: str) -> bool:
        return bool(re.search(r"\.{6,}\s*\d+\s*$", t))

    def _find_line(self, lines: list[_Line], pat: re.Pattern) -> int | None:
        for i, ln in enumerate(lines):
            if self._is_tocish_line(ln.text):
                continue
            if pat.search(ln.text):
                return i
        return None

    def _find_next_chapter(self, lines: list[_Line], start: int) -> int | None:
        for i in range(start, len(lines)):
            if self._is_tocish_line(lines[i].text):
                continue
            if self.chapter_re.search(lines[i].text):
                return i
        return None

    def _find_next_section_like(self, lines: list[_Line], start: int, upper: int) -> int | None:
        pat = re.compile(r"^[一二三四五六七八九十0-9]+[、.．]\s*")
        for i in range(start, upper):
            if self._is_tocish_line(lines[i].text):
                continue
            if pat.search(lines[i].text):
                return i
        return None
