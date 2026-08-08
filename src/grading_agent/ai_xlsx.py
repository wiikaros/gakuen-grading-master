"""AI 评分结果 xlsx：写到 output/<任务>/ 下的独立副本，绝不碰 tasks/ 原始文件。

为什么必须隔离（2026-08-08 修复）：
原先跑批与改分都直接写 tasks/<任务>/xxx.xlsx 的「阅卷人评分」列，造成两个后果——
1. 覆盖真人评分，原始阅卷记录被破坏且不可恢复
2. read_scores_by_key() 读的正是该列，于是"人工分"读回的是 AI 自己写的分，
   偏差恒为 0，一致性验证基准彻底失效

现在的约定：
- tasks/<任务>/*.xlsx  ——只读输入，任何情况下不写
- output/<任务>/<原名>_AI评分.xlsx ——AI 评分与人工改分的唯一写入目标
  首次使用时从原始 xlsx 复制而来（保留全部列与格式），之后只更新评分列
"""
from __future__ import annotations

import shutil
from pathlib import Path

AI_XLSX_SUFFIX = "_AI评分"


def ai_xlsx_path(source_xlsx: Path, output_dir: Path) -> Path:
    """AI 评分副本应在的位置：output/<任务>/<原名>_AI评分.xlsx。"""
    return output_dir / f"{source_xlsx.stem}{AI_XLSX_SUFFIX}.xlsx"


def ensure_ai_xlsx(source_xlsx: Path, output_dir: Path) -> Path:
    """确保 AI 评分副本存在，不存在则从原始 xlsx 复制一份。返回副本路径。

    用 shutil.copy2 整份复制而非 openpyxl 重写，以保留原表的列宽、格式与其他工作表。
    已存在时**不覆盖**——副本里可能已有此前跑批与人工改分的结果。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    target = ai_xlsx_path(source_xlsx, output_dir)
    if not target.exists():
        shutil.copy2(source_xlsx, target)
    return target


def reset_ai_xlsx(source_xlsx: Path, output_dir: Path) -> Path:
    """丢弃已有副本，从原始 xlsx 重新复制一份干净的。返回副本路径。"""
    target = ai_xlsx_path(source_xlsx, output_dir)
    if target.exists():
        target.unlink()
    return ensure_ai_xlsx(source_xlsx, output_dir)
