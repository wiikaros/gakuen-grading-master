"""LLM 抽象接口。

PRD §6.6: 两个方法——分类(identify) 和 评分(grade)。
"""
from __future__ import annotations

from typing import Protocol

from grading_agent.models import (
    GradeResult,
    IdentificationResult,
    StandardBundle,
    TaskTriple,
)


class LLMBackend(Protocol):
    """LLM 后端抽象接口。"""

    def classify_problem(
        self,
        description: str,
        code: str,
        candidates_summary: str,
        candidates: list[TaskTriple],
    ) -> IdentificationResult:
        """题目识别：1-of-N 分类，返回三元组+置信度+候选。

        candidates_summary 是喂给 prompt 的候选概览文本；
        candidates 是与 summary 同序的三元组列表，用于把 LLM 输出的候选编号映射回三元组。
        """
        ...

    def grade_answer(
        self,
        bundle: StandardBundle,
        description: str,
        student_code: str,
    ) -> GradeResult:
        """评分：返回维度分项+扣分+汇总+置信度。"""
        ...
