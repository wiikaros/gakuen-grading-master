"""单 URL 抓取调试脚本。

用法：
    python scripts/fetch_one.py "<URL>"
    python scripts/fetch_one.py "<URL>" --debug   # 写 render_dumps

验证阶段1：确认单 URL 稳定返回非空 description/code。
URL 可手动从 xlsx 中复制（阶段3未完成时无需依赖 xlsx 模块）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让脚本能 import src 下的包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grading_agent.url_fetcher import UrlFetcher


def main() -> int:
    parser = argparse.ArgumentParser(description="单 URL 抓取调试")
    parser.add_argument("url", help="阅卷答卷 URL")
    parser.add_argument("--debug", action="store_true", help="写 render_dumps 转储")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（调试用）")
    parser.add_argument("--timeout", type=int, default=20000, help="超时毫秒")
    args = parser.parse_args()

    dumps_dir = Path("output/_debug/render_dumps") if args.debug else None

    print(f"抓取 URL: {args.url[:80]}...")
    with UrlFetcher(
        headless=not args.headed,
        timeout_ms=args.timeout,
        debug_dumps_dir=dumps_dir,
        always_dump=args.debug,
    ) as fetcher:
        result = fetcher.fetch(args.url, row_id="debug")

    if not result.ok:
        print(f"\n❌ 抓取失败: {result.error}")
        return 1

    print("\n✅ 抓取成功")
    print("\n" + "=" * 60)
    print("【题目描述】")
    print("=" * 60)
    print(result.description or "(空)")
    print("\n" + "=" * 60)
    print("【学生代码】")
    print("=" * 60)
    print(result.code or "(空)")

    if dumps_dir:
        print(f"\n转储已写入: {dumps_dir}")

    # 简单非空校验
    if not result.description or not result.code:
        print("\n⚠ 警告: 题目描述或代码为空，请检查选择器是否仍然命中")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
