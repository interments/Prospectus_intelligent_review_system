from __future__ import annotations

import re

from ..schemas.models import TextBlock

MAIN_SECTION_PATTERNS = [r"发行人基本情况", r"公司基本情况"]
STRONG_MAIN_PATTERN = r"第[一二三四五六七八九十0-9]+节[^\n]{0,30}(发行人基本情况|公司基本情况)"
SUB_SECTION_KEYWORDS = ["股本", "股权", "变动", "增资", "转让", "资本"]
NEXT_SECTION_PATTERNS = [r"业务与技术", r"同业竞争", r"关联交易", r"董事", r"监事", r"高级管理人员"]
STRONG_NEXT_PATTERN = r"第[一二三四五六七八九十0-9]+节"


class SectionLocator:
    def locate_candidate_pages(self, text_blocks: list[TextBlock]) -> list[TextBlock]:
        selected: list[TextBlock] = []

        # 先定位父章节“第X节 发行人基本情况/公司基本情况”（取最后一次命中，规避目录页）
        candidate_parent_indices = []
        for i, block in enumerate(text_blocks):
            text = block.text
            if block.page <= 15:
                continue
            if self._is_toc_page(text):
                continue
            if re.search(STRONG_MAIN_PATTERN, text):
                candidate_parent_indices.append(i)

        if not candidate_parent_indices:
            # 找不到强匹配再降级弱匹配
            for i, block in enumerate(text_blocks):
                text = block.text
                if block.page <= 15:
                    continue
                if self._is_toc_page(text):
                    continue
                if any(re.search(p, text) for p in MAIN_SECTION_PATTERNS):
                    candidate_parent_indices.append(i)

        start_idx = candidate_parent_indices[-1] if candidate_parent_indices else None
        end_idx = len(text_blocks)
        if start_idx is not None:
            for j in range(start_idx + 1, len(text_blocks)):
                text = text_blocks[j].text
                if self._is_toc_page(text):
                    continue
                # 进入下一“第X节”即视为父章节结束
                if re.search(STRONG_NEXT_PATTERN, text):
                    end_idx = j
                    break

            for block in text_blocks[start_idx:end_idx]:
                text = block.text
                if self._is_toc_page(text):
                    continue
                if any(k in text for k in SUB_SECTION_KEYWORDS):
                    selected.append(block)

        # fallback: still allow keyword pages, but drop likely TOC pages
        if not selected:
            selected = [
                b
                for b in text_blocks
                if b.page > 15 and (not self._is_toc_page(b.text)) and any(k in b.text for k in SUB_SECTION_KEYWORDS)
            ]
        return selected

    @staticmethod
    def _is_toc_page(text: str) -> bool:
        # 目录页特征：大量点线 + 行尾页码
        dot_leader = len(re.findall(r"\.{6,}", text))
        numbered_lines = len(re.findall(r"\n\s*[^\n]{2,80}\s+\d+\s*$", text, flags=re.MULTILINE))
        has_catalog = ("目录" in text[:200]) or ("目 录" in text[:200])
        return has_catalog or (dot_leader >= 5 and numbered_lines >= 5)
