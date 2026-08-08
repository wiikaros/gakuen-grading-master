"""LangChain @tool 定义（预留）。

PRD §7.1: 本期编排器直接调用确定性函数，不走 ReAct agent。
这些 @tool 装饰是为未来升级为对话式 ReAct agent 预留的接口，本期不启用。

如需升级为 ReAct agent，可把这些 tool 喂给 langchain 的 create_react_agent。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from grading_agent.models import FetchResult, GradeResult, IdentificationResult, StandardBundle, TaskTriple


def fetch_url(url: str, row_id: str = "") -> "FetchResult":
    """Playwright 抓题目描述+学生代码（预留 tool，本期由编排器直接调用 UrlFetcher）。"""
    raise NotImplementedError("本期由编排器直接调用 UrlFetcher.fetch，不走 @tool")


def get_standard_bundle(triple: "TaskTriple") -> "StandardBundle":
    """取该题标准答案+评分标准（预留 tool）。"""
    raise NotImplementedError("本期由编排器直接调用 StandardLibrary.get")


def list_candidates() -> list["TaskTriple"]:
    """列全部候选三元组（预留 tool）。"""
    raise NotImplementedError("本期由编排器直接调用 StandardLibrary.all_triples")


def identify_problem(description: str, code: str, candidates: str) -> "IdentificationResult":
    """LLM 1-of-N 分类（预留 tool）。"""
    raise NotImplementedError("本期由 ProblemIdentifier.identify 直接调用")


def grade_answer(bundle: "StandardBundle", description: str, student_code: str) -> "GradeResult":
    """LLM 评分（预留 tool）。"""
    raise NotImplementedError("本期由 Grader.fetch_and_grade 直接调用")


def write_score(row_num: int, score: int) -> dict:
    """回写 xlsx 评分列（预留 tool）。"""
    raise NotImplementedError("本期由编排器直接调用 update_cell_in_memory + save_workbook")
