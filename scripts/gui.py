"""GUI 启动入口。

用法：
    python scripts/gui.py

两个页签：
- 跑批监控台：选任务 → 预检 → 跑批（实时进度/日志/阶段B确认弹窗/停止）
- 结果复核：浏览历史评分结果、筛选、逐行看详情、改分回写、单行重评

注：浏览历史结果不需要 API key，也不花钱；只有跑批与重评会调用 LLM。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import src 下的包（与 scripts/fetch_one.py 同模式）
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grading_agent.gui.app import main


if __name__ == "__main__":
    sys.exit(main())
