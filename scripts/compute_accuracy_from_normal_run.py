"""基于「普通模式」已有的 grading_detail.jsonl + 原始 xlsx 人工分，直接算一致率（不重跑评分）。

用法：
    python scripts/compute_accuracy_from_normal_run.py <任务文件夹> [--detail-log <jsonl>] [--output-dir <目录>]

示例：
    python scripts/compute_accuracy_from_normal_run.py tasks/6.14
    python scripts/compute_accuracy_from_normal_run.py tasks/6.14 --detail-log output/6.14/grading_detail.jsonl

说明：
    普通模式跑批会写 output/<任务名>/grading_detail.jsonl（AI 分），原始 xlsx 的 G 列人工分
    始终未被改动。本脚本把它们按 (answer_id, task_id) join 起来算一致率，无需重跑评分。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from grading_agent.verify.consistency import run_consistency_check


def main() -> int:
    parser = argparse.ArgumentParser(
        description="基于普通模式跑批产物算一致率（不重跑评分）",
    )
    parser.add_argument("task_dir", help="任务文件夹路径（含原始 xlsx 人工分，如 tasks/6.14）")
    parser.add_argument("--detail-log", default=None,
                        help="普通模式跑批的 grading_detail.jsonl 路径（默认 output/<任务名>/grading_detail.jsonl）")
    parser.add_argument("--output-dir", default=None,
                        help="报告输出目录（默认 output/<任务名>/）")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).resolve()
    task_name = task_dir.name
    detail_log = (Path(args.detail_log).resolve() if args.detail_log
                  else ROOT / "output" / task_name / "grading_detail.jsonl")
    output_dir = (Path(args.output_dir).resolve() if args.output_dir
                  else ROOT / "output" / task_name)

    print(f"使用 AI 明细: {detail_log}  (exists={detail_log.exists()})")
    print(f"人工分来源  : {task_dir}  (原始 xlsx，只读)")
    print()

    run_consistency_check(
        task_dir=task_dir,
        detail_log_path=detail_log,
        output_dir=output_dir,
        print_fn=print,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
