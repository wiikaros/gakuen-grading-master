"""跑 7.12 一致性验证的便捷入口。

用法：
    python scripts/run_verify.py [tasks/7.12] [--skip-identification]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grading_agent.cli import main as cli_main


def main() -> int:
    parser = argparse.ArgumentParser(description="一致性验证便捷入口")
    parser.add_argument("task_dir", nargs="?", default="tasks/7.12", help="任务文件夹（默认 tasks/7.12）")
    parser.add_argument("--skip-identification", action="store_true",
                        help="跳过题目识别，从已有 jsonl 恢复（用于 prompt 调优后回归）")
    parser.add_argument("--max-rows", type=int, default=None, help="只跑前N行（调试）")
    args = parser.parse_args()

    # 构造 cli 参数
    cli_argv = [args.task_dir, "--verify"]
    if args.skip_identification:
        cli_argv.append("--skip-identification")
    if args.max_rows:
        cli_argv.extend(["--max-rows", str(args.max_rows)])

    sys.argv = [sys.argv[0]] + cli_argv
    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
