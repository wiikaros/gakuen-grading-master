"""全部数据结构定义。

分两层：
- 内部数据层：中文枚举 + dataclass（语义清晰，业务逻辑使用）
- LLM 输出层：英文枚举 + Pydantic BaseModel（兼容 function calling）

两层之间通过转换函数桥接（见 llm/langchain_backend.py）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


# ── 枚举类型 ──

Level = Literal["一级", "二级"]
Session = Literal["上午", "下午"]
QuestionNo = Literal[1, 2]


# ── 标准库相关 ──

@dataclass(frozen=True)
class TaskTriple:
    """题目三元组：(级别, 场次, 题号)，唯一定位一道题。"""
    level: Level
    session: Session
    question: QuestionNo

    def __str__(self) -> str:  # "一级/上午/题1"
        return f"{self.level}/{self.session}/题{self.question}"


@dataclass
class Dimension:
    """评分维度。如"有定义变量"(5分)。"""
    name: str          # "有定义变量"
    max_score: int     # 5
    description: str   # 完整维度描述


@dataclass
class StandardEntry:
    """一道题的评分标准。"""
    triple: TaskTriple
    total_max: int               # 25
    dimensions: list[Dimension]  # 5个
    special_notes: str           # "特别说明"原文（阶梯分档规则），可能为空
    syntax_penalty_rule: str     # "语法错误/拼写错误每处扣1分"


@dataclass
class StandardBundle:
    """一道题的标准答案 + 评分标准，喂给评分 LLM。"""
    triple: TaskTriple
    standard: StandardEntry
    reference_code: str          # 标准答案 C++ 代码


# ── 任务行 ──

@dataclass
class TaskRow:
    """xlsx 一行记录。注意：不含 score 字段——验证模式根本不读；生产模式评分是输出。"""
    row_num: int                 # xlsx 行号（1-based，含表头偏移）
    user_id: str
    answer_id: str
    task_id: str                 # 题目ID（如"7070"）
    url: str
    metadata: dict = field(default_factory=dict)  # 赛区ID等


# ── 抓取结果 ──

@dataclass
class FetchResult:
    """URL 抓取结果。"""
    description: str
    code: str
    raw_html: str | None
    ok: bool
    error: str | None
    row_id: str = ""


# ── 识别结果（内部层）──

@dataclass
class IdentificationResult:
    """题目识别结果。candidates 用 dict 列表而非 tuple，便于 JSON 序列化。"""
    best_triple: TaskTriple
    confidence: float            # 0.0-1.0
    candidates: list[tuple[TaskTriple, float]]   # 全部候选按置信度降序
    reasoning: str
    parse_ok: bool = True


# ── 评分结果（内部层）──

@dataclass
class DimensionScore:
    """单个维度的评分。"""
    name: str
    awarded: int
    max_score: int
    reasoning: str               # 该维度给分理由（1句）


@dataclass
class SyntaxPenalty:
    """语法扣分。按错误类别扣分，每类 1 分（不按出现次数重复扣）。"""
    error_categories: list[str]    # 每类错误的描述（如"拼写错误"、"分号缺失"）
    category_count: int            # 出现的错误类别数
    total_deduction: int           # = category_count（每类扣1分）


@dataclass
class GradeResult:
    """评分结果。"""
    triple: TaskTriple
    dimension_scores: list[DimensionScore]  # 5个
    syntax_penalty: SyntaxPenalty
    subtotal: int                # 维度合计（0-25）
    total_score: int             # max(0, min(25, subtotal - deduction))
    reasoning: str               # 整体理由（2-3句）
    confidence: float            # 0.0-1.0
    parse_ok: bool = True


# ── 运行报告 ──

@dataclass
class RunReport:
    """一次任务的运行统计。"""
    total_rows: int
    fetched_ok: int
    fetch_failed: int
    high_confidence_written: int
    low_confidence_queued: int
    detail_log_path: Path
    review_queue_path: Path
    output_xlsx_path: Path


# ── LLM 输出层模型（Pydantic BaseModel，英文枚举，兼容 function calling）──
# 这些类延迟导入 pydantic，避免在仅需 dataclass 的场景强制依赖 pydantic。

def _import_pydantic_models():
    """延迟导入 Pydantic 模型，避免顶层导入 pydantic。"""
    from pydantic import BaseModel
    from typing import List

    class IdentificationOutput(BaseModel):
        """识别结果 schema。英文标签避免中文枚举在 function calling 中不稳定。"""
        level: Literal["L1", "L2"]
        session: Literal["morning", "afternoon"]
        question: int               # 1 or 2
        confidence: float
        reasoning: str

    class DimensionScoreOutput(BaseModel):
        name: str                   # 维度名
        awarded: int
        max_score: int
        reasoning: str

    class GradeOutput(BaseModel):
        """评分结果 schema。"""
        dimension_scores: List[DimensionScoreOutput]
        error_categories: List[str]     # 语法错误类别，每类 1 分
        total_deduction: int
        subtotal: int
        total_score: int
        reasoning: str
        confidence: float

    return IdentificationOutput, GradeOutput, DimensionScoreOutput


# 便捷访问：首次访问时构造
def get_llm_output_models():
    """返回 (IdentificationOutput, GradeOutput, DimensionScoreOutput)。"""
    return _import_pydantic_models()


# ── docx 解析中间结构 ──

@dataclass
class DocxContent:
    """docx 解析的中间结构：段落列表 + 表格列表。"""
    paragraphs: list[dict]      # 每段 {text: str, style: str, bold: bool}
    tables: list[list[list[str]]]  # 表格列表，每个表格是 list[list[str]]


# ── 枚举转换辅助 ──

LEVEL_MAP = {"L1": "一级", "L2": "二级"}
SESSION_MAP = {"morning": "上午", "afternoon": "下午"}
LEVEL_MAP_REV = {v: k for k, v in LEVEL_MAP.items()}
SESSION_MAP_REV = {v: k for k, v in SESSION_MAP.items()}


def identification_output_to_result(out) -> IdentificationResult:
    """IdentificationOutput(Pydantic) → IdentificationResult(dataclass)。

    candidates 字段在 LLM 输出层不含（function calling 难以稳定输出排序候选列表），
    这里只构造 best_triple；如需候选排名，由调用方在 prompt 要求中额外解析。
    """
    triple = TaskTriple(
        level=LEVEL_MAP[out.level],
        session=SESSION_MAP[out.session],
        question=out.question,  # type: ignore[arg-type]
    )
    return IdentificationResult(
        best_triple=triple,
        confidence=out.confidence,
        candidates=[(triple, out.confidence)],
        reasoning=out.reasoning,
        parse_ok=True,
    )


def grade_output_to_result(out, triple: TaskTriple) -> GradeResult:
    """GradeOutput(Pydantic) → GradeResult(dataclass)。"""
    dimension_scores = [
        DimensionScore(
            name=d.name,
            awarded=d.awarded,
            max_score=d.max_score,
            reasoning=d.reasoning,
        )
        for d in out.dimension_scores
    ]
    syntax_penalty = SyntaxPenalty(
        error_categories=list(out.error_categories),
        category_count=len(out.error_categories),
        total_deduction=out.total_deduction,
    )
    return GradeResult(
        triple=triple,
        dimension_scores=dimension_scores,
        syntax_penalty=syntax_penalty,
        subtotal=out.subtotal,
        total_score=out.total_score,
        reasoning=out.reasoning,
        confidence=out.confidence,
        parse_ok=True,
    )
