"""LLM 抽象接口。

PRD §6.6: 两个方法——分类(identify) 和 评分(grade)。
"""
from __future__ import annotations

from typing import Protocol

from grading_agent.models import (
    GradeResult,
    IdentificationResult,
    StandardBundle,
)


class LLMBackend(Protocol):
    """LLM 后端抽象接口。"""

    def classify_problem(
        self,
        description: str,
        code: str,
        candidates_summary: str,
    ) -> IdentificationResult:
        """题目识别：1-of-N 分类，返回三元组+置信度+候选。"""
        ...

    def grade_answer(
        self,
        bundle: StandardBundle,
        description: str,
        student_code: str,
    ) -> GradeResult:
        """评分：返回维度分项+扣分+汇总+置信度。"""
        ...
