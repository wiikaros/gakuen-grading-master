"""CLI 入口：argparse + 阶段 A-D 编排。

PRD §6.8 + §9.1：
    python -m grading_agent.cli "tasks/7.12" --provider deepseek --model deepseek-chat
    python -m grading_agent.cli tasks/7.12 --max-rows 5 --debug-fetch
    python -m grading_agent.cli tasks/7.12 --verify --provider deepseek
    python -m grading_agent.cli tasks/7.12 --verify --skip-identification
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from grading_agent.config import load_config
from grading_agent.agent.orchestrator import Orchestrator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="grading_agent.cli",
        description="基于 LLM 的自动化阅卷工具（CLI 批处理）",
    )
    parser.add_argument("task_dir", help="任务文件夹路径（含 xlsx + 3 个 docx）")
    parser.add_argument("--model", default=None, help="LLM 模型名（默认从 config/env 读）")
    parser.add_argument(
        "--provider", default=None,
        choices=["deepseek", "openai", "claude"],
        help="LLM 提供方（默认从 config/env 读）",
    )
    parser.add_argument("--confidence-threshold", type=float, default=None,
                        help="高/低置信度分流阈值（默认 0.7）")
    parser.add_argument("--max-rows", type=int, default=None, help="调试:只跑前N行")
    parser.add_argument("--debug-fetch", action="store_true", help="每次抓取都写 render_dumps")
    parser.add_argument("--skip-identification", action="store_true",
                        help="跳过阶段B题目识别，从已有 grading_detail.jsonl 恢复题目ID→三元组映射")
    parser.add_argument("--force-rerun", action="store_true",
                        help="忽略已完成行，全部重新评分（默认会断点续跑跳过已评分的行）")
    parser.add_argument("--verify", action="store_true", help="一致性验证模式(不写回xlsx)")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    task_dir = Path(args.task_dir)
    if not task_dir.exists() or not task_dir.is_dir():
        print(f"❌ 任务文件夹不存在或不是目录: {task_dir}", file=sys.stderr)
        return 1

    # 加载配置
    config = load_config()

    # CLI 参数覆盖 config
    if args.provider:
        config.llm.provider = args.provider
    if args.model:
        config.llm.model = args.model
    if args.confidence_threshold is not None:
        config.grading.confidence_threshold = args.confidence_threshold

    # verify 模式下默认不写回
    if args.verify:
        config.grading.write_back = False

    # API key 校验
    if config.llm.provider == "deepseek" and not config.deepseek_api_key:
        print("❌ 未配置 DEEPSEEK_API_KEY，请复制 .env.example 为 .env 并填入 API key", file=sys.stderr)
        return 1
    if config.llm.provider == "openai" and not config.openai_api_key:
        print("❌ 未配置 OPENAI_API_KEY", file=sys.stderr)
        return 1
    if config.llm.provider == "claude" and not config.anthropic_api_key:
        print("❌ 未配置 ANTHROPIC_API_KEY", file=sys.stderr)
        return 1

    print(f"=" * 60)
    print(f"  学园阅卷大师 — CLI 批处理")
    print(f"  任务: {task_dir}")
    print(f"  模型: {config.llm.provider}/{config.llm.model}")
    print(f"  置信度阈值: {config.grading.confidence_threshold}")
    if args.verify:
        print(f"  模式: 一致性验证（不写回 xlsx）")
    if args.max_rows:
        print(f"  限制: 仅前 {args.max_rows} 行")
    if args.skip_identification:
        print(f"  选项: 跳过题目识别（从 JSONL 恢复）")
    print(f"=" * 60)

    orchestrator = Orchestrator(
        task_dir=task_dir,
        config=config,
        max_rows=args.max_rows,
        debug_fetch=args.debug_fetch,
        skip_identification=args.skip_identification,
        verify=args.verify,
        force_rerun=args.force_rerun,
    )

    try:
        if args.verify:
            # verify 模式：跑完后做一致性对比
            report = orchestrator.run()
            if report.total_rows == 0:
                return 1
            # 调用一致性验证
            from grading_agent.verify.consistency import run_consistency_check
            run_consistency_check(task_dir, orchestrator.detail_log_path, orchestrator.output_dir)
        else:
            report = orchestrator.run()
            if report.total_rows == 0:
                return 1
    except KeyboardInterrupt:
        print("\n\n⚠ 用户中断（Ctrl+C）。已完成行已写入 JSONL，重跑可断点续跑。", file=sys.stderr)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
