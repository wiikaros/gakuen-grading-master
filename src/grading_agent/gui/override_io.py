"""human_override.jsonl 读写 + 与 grading_detail.jsonl 的合并视图。

设计要点（见计划文件「改分与重评的数据流」）：
- grading_detail.jsonl 只由 AI 追加，是断点续跑源与一致性验证基准，人工改分绝不写入
- human_override.jsonl 只记人工改动流水，追加而非覆写；同一行改两次留两条，最后一条生效
- 撤销 = 追加一条反向记录（new_score 回到改动前的值），因此历史完整可追溯

为什么分开：consistency.py 只读 grading_detail.jsonl。若把人工修正写进去，
调完 prompt 重跑验证时一致率会被人工修正虚高，失去衡量 AI 真实水平的意义。
"""
from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

OVERRIDE_FILENAME = "human_override.jsonl"


# ── 写入 ──

def append_override(
    path: Path,
    *,
    row_num: int,
    answer_id: str,
    task_id: str,
    old_score: Any,
    new_score: int,
    source: str = "ai",
    note: str = "",
    timestamp: str | None = None,
) -> dict:
    """追加一条人工改分记录，返回写入的 dict。

    Args:
        old_score: 改动前生效的分数（AI 分或上一次人工分），可能为 None（原本没分）
        source: "ai" 表示改的是 AI 原始分；"human" 表示改的是上一次人工分
        note: 改分备注（可空）
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "row_num": row_num,
        "answer_id": answer_id,
        "task_id": task_id,
        "old_score": old_score,
        "new_score": new_score,
        "source": source,
        "note": note,
        "timestamp": timestamp or _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
    return record


def read_overrides(path: Path) -> list[dict]:
    """读全部改动流水（按写入顺序）。文件不存在返回空列表，损坏行跳过。"""
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def latest_override_by_row(path: Path) -> dict[int, dict]:
    """每行取最后一条生效的改动。返回 {row_num: record}。"""
    latest: dict[int, dict] = {}
    for rec in read_overrides(path):
        rn = rec.get("row_num")
        if isinstance(rn, int):
            latest[rn] = rec
    return latest


# ── 合并视图（表格数据源）──

@dataclass
class RowView:
    """表格一行的展示模型：AI 评分 + 人工改动 + 人工原始分 的合并结果。"""
    row_num: int
    answer_id: str = ""
    task_id: str = ""
    triple: str = ""
    url: str = ""

    # AI 原始输出（来自 grading_detail.jsonl）
    ai_score: int | None = None
    confidence: float | None = None
    parse_ok: bool = True
    dimension_scores: list[dict] = field(default_factory=list)
    error_categories: list[str] = field(default_factory=list)
    total_deduction: int = 0
    subtotal: int = 0
    ai_reasoning: str = ""
    timestamp: str = ""

    # 抓取到的原始答卷（2026-08 起随评分一并存入 jsonl）。
    # 早期 jsonl 无这两个字段，此时为空串 → 详情面板提示可点重评补抓。
    description: str = ""
    student_code: str = ""

    @property
    def has_content(self) -> bool:
        """jsonl 是否存有题目描述/学生代码（旧记录没有）。"""
        return bool(self.description or self.student_code)

    # xlsx 里的人工评分（一致性对比用）
    human_score: int | None = None

    # 人工改动（来自 human_override.jsonl）
    override_score: int | None = None
    override_note: str = ""
    override_time: str = ""

    @property
    def effective_score(self) -> int | None:
        """当前生效分数：人工改过则以改动为准，否则用 AI 分。"""
        return self.override_score if self.override_score is not None else self.ai_score

    @property
    def is_overridden(self) -> bool:
        return self.override_score is not None

    @property
    def diff(self) -> int | None:
        """生效分 − 人工原始分。任一缺失返回 None。"""
        eff = self.effective_score
        if eff is None or self.human_score is None:
            return None
        return eff - self.human_score

    def status(self, threshold: float) -> str:
        """状态标签，用于表格状态列与着色。"""
        if not self.parse_ok:
            return "失败"
        if self.ai_score is None:
            return "未评"
        if self.confidence is not None and self.confidence < threshold:
            return "待复核"
        return ""


def read_detail_rows(path: Path) -> list[dict]:
    """读 grading_detail.jsonl 全部记录（按写入顺序）。

    同一 row_num 可能出现多次（多轮 verify 追加），调用方决定取哪条。
    """
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def build_row_views(
    detail_path: Path,
    override_path: Path,
    human_scores: dict[tuple[str, str], int | None] | None = None,
    url_by_row: dict[int, str] | None = None,
) -> list[RowView]:
    """合并三方数据成表格视图，按 row_num 升序。

    Args:
        detail_path: grading_detail.jsonl（AI 输出）
        override_path: human_override.jsonl（人工改动）
        human_scores: read_scores_by_key() 的结果 {(answer_id, task_id): 人工分}
        url_by_row: {row_num: url}，来自 xlsx，供「打开网页」按钮用

    同一 row_num 多条 AI 记录时取最后一条（最近一次评分结果）。
    """
    human_scores = human_scores or {}
    url_by_row = url_by_row or {}

    by_row: dict[int, RowView] = {}
    for rec in read_detail_rows(detail_path):
        rn = rec.get("row_num")
        if not isinstance(rn, int):
            continue
        answer_id = str(rec.get("answer_id", ""))
        task_id = str(rec.get("task_id", ""))
        # 后写的覆盖先写的：同 row_num 保留最近一次评分
        by_row[rn] = RowView(
            row_num=rn,
            answer_id=answer_id,
            task_id=task_id,
            triple=str(rec.get("triple", "")),
            url=url_by_row.get(rn, ""),
            ai_score=rec.get("total_score"),
            confidence=rec.get("confidence"),
            parse_ok=bool(rec.get("parse_ok", True)),
            dimension_scores=list(rec.get("dimension_scores") or []),
            error_categories=list(rec.get("error_categories") or []),
            total_deduction=int(rec.get("total_deduction") or 0),
            subtotal=int(rec.get("subtotal") or 0),
            ai_reasoning=str(rec.get("reasoning", "")),
            timestamp=str(rec.get("timestamp", "")),
            description=str(rec.get("description") or ""),
            student_code=str(rec.get("student_code") or ""),
            human_score=human_scores.get((answer_id, task_id)),
        )

    # 叠加人工改动
    for rn, rec in latest_override_by_row(override_path).items():
        view = by_row.get(rn)
        if view is None:
            # 改动指向的行没有 AI 记录（罕见），仍建一条以免丢失改动
            view = RowView(
                row_num=rn,
                answer_id=str(rec.get("answer_id", "")),
                task_id=str(rec.get("task_id", "")),
                url=url_by_row.get(rn, ""),
            )
            view.human_score = human_scores.get((view.answer_id, view.task_id))
            by_row[rn] = view
        view.override_score = rec.get("new_score")
        view.override_note = str(rec.get("note", ""))
        view.override_time = str(rec.get("timestamp", ""))

    return [by_row[k] for k in sorted(by_row)]
