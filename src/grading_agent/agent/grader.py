"""评分：LLM 结构化输出 + 置信度分流。

PRD §4 阶段 C：取参考代码+评分标准 → LLM 评分 → 高置信度写回/低置信度留空。
"""
from __future__ import annotations

from grading_agent.models import (
    FetchResult,
    GradeResult,
    TaskTriple,
)
from grading_agent.standard_library import StandardLibrary
from grading_agent.llm.base import LLMBackend
from grading_agent.url_fetcher import UrlFetcher


class Grader:
    """评分器：抓取 + LLM 评分。"""

    def __init__(self, fetcher: UrlFetcher, llm: LLMBackend, stdlib: StandardLibrary) -> None:
        self.fetcher = fetcher
        self.llm = llm
        self.stdlib = stdlib

    def fetch_and_grade(
        self,
        url: str,
        triple: TaskTriple,
        row_id: str = "",
    ) -> tuple[GradeResult, FetchResult]:
        """抓取 URL + 按已知三元组评分。

        Returns: (评分结果, 抓取结果)。抓取失败时 result.parse_ok=False, confidence=0。
        """
        fetch_result = self.fetcher.fetch(url, row_id=row_id)
        if not fetch_result.ok:
            from grading_agent.models import SyntaxPenalty
            return GradeResult(
                triple=triple,
                dimension_scores=[],
                syntax_penalty=SyntaxPenalty(error_categories=[], category_count=0, total_deduction=0),
                subtotal=0,
                total_score=0,
                reasoning=f"抓取失败: {fetch_result.error}",
                confidence=0.0,
                parse_ok=False,
            ), fetch_result

        try:
            bundle = self.stdlib.get(triple)
        except KeyError:
            from grading_agent.models import SyntaxPenalty
            return GradeResult(
                triple=triple,
                dimension_scores=[],
                syntax_penalty=SyntaxPenalty(error_categories=[], category_count=0, total_deduction=0),
                subtotal=0,
                total_score=0,
                reasoning=f"三元组 {triple} 无评分标准",
                confidence=0.0,
                parse_ok=False,
            ), fetch_result

        result = self.llm.grade_answer(
            bundle=bundle,
            description=fetch_result.description,
            student_code=fetch_result.code,
        )
        return result, fetch_result

    @staticmethod
    def is_high_confidence(result: GradeResult, threshold: float) -> bool:
        """是否高置信度（可直写 xlsx）。"""
        return result.parse_ok and result.confidence >= threshold
