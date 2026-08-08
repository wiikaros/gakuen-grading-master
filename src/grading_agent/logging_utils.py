"""JSONL 明细日志 + 待复核清单导出 + 断点续跑支持。

PRD §6.9:
- write_grade_detail: 追加 JSONL（每行评分明细），每次 flush
- write_review_queue: CSV（待复核清单）
- read_completed_rows: 从已有 JSONL 提取已完成行号集合（断点续跑）
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Iterable

from grading_agent.models import GradeResult, TaskRow


def write_grade_detail(
    path: Path,
    row: TaskRow,
    result: GradeResult,
    timestamp: str = "",
    description: str = "",
    student_code: str = "",
) -> None:
    """追加一行 JSONL：行号/答卷ID/题目ID/三元组/维度分项/扣分/总分/置信度/理由/时间戳。

    另存抓取到的题目描述与学生代码（description/student_code），
    使复核界面无需重新抓取即可查看原始答卷——重抓一次要几秒且占用浏览器，
    存下来后浏览历史结果是纯本地读取。

    每次写入后 flush，保证断点续跑时 JSONL 是可靠的恢复源。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "row_num": row.row_num,
        "answer_id": row.answer_id,
        "task_id": row.task_id,
        "triple": str(result.triple),
        "description": description,
        "student_code": student_code,
        "dimension_scores": [
            {"name": d.name, "awarded": d.awarded, "max_score": d.max_score, "reasoning": d.reasoning}
            for d in result.dimension_scores
        ],
        "error_categories": list(result.syntax_penalty.error_categories),
        "total_deduction": result.syntax_penalty.total_deduction,
        "subtotal": result.subtotal,
        "total_score": result.total_score,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "parse_ok": result.parse_ok,
        "timestamp": timestamp,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def read_completed_rows(path: Path) -> set[int]:
    """从已有 JSONL 提取已完成的 row_num 集合。

    用于断点续跑：重启时跳过这些行，避免重复花 LLM 钱。
    文件不存在或为空返回空集。损坏的行（JSON 解析失败）跳过。
    """
    if not path.exists():
        return set()
    completed: set[int] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                rn = rec.get("row_num")
                if isinstance(rn, int):
                    completed.add(rn)
            except json.JSONDecodeError:
                continue
    return completed


def read_triple_map_from_detail(path: Path) -> dict[str, str]:
    """从已有 JSONL 提取 task_id → triple 字符串 的映射。

    用于 --skip-identification：跳过阶段 B 时从历史结果恢复识别缓存。
    取每个 task_id 第一次出现的 triple。
    """
    if not path.exists():
        return {}
    triple_map: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                tid = rec.get("task_id")
                triple = rec.get("triple")
                if tid and triple and tid not in triple_map:
                    triple_map[tid] = triple
            except json.JSONDecodeError:
                continue
    return triple_map


def write_review_queue(path: Path, rows: Iterable[dict]) -> None:
    """导出待复核清单 CSV。

    每行 dict 字段：row_num/answer_id/task_id/url/confidence/ai_total_score/reason/reasoning
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["row_num", "answer_id", "task_id", "url", "confidence", "ai_total_score", "reason", "reasoning"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            # 只写已知字段，缺失的填空
            writer.writerow({k: r.get(k, "") for k in fieldnames})
