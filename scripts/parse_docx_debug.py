"""docx 解析结果打印调试脚本。

用法：
    python scripts/parse_docx_debug.py <任务文件夹>

打印任务文件夹里所有级别的评分标准 + 参考代码，并校验 key 集合一致性。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grading_agent.docx_parser import parse_standards
from grading_agent.models import level_sort_key
from grading_agent.reference_codes import parse_reference_codes
from grading_agent.standard_library import find_task_docxs


def main() -> int:
    parser = argparse.ArgumentParser(description="docx 解析调试")
    parser.add_argument("task_dir", help="任务文件夹路径")
    args = parser.parse_args()
    task_dir = Path(args.task_dir)

    try:
        standards_map, ref_path = find_task_docxs(task_dir)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        return 1

    for level in sorted(standards_map.keys(), key=level_sort_key):
        print(f"{level}标准: {standards_map[level].name}")
    print(f"参考代码: {ref_path.name}")

    # 解析各级评分标准
    all_entries = []
    for level in sorted(standards_map.keys(), key=level_sort_key):
        print("\n" + "=" * 60)
        print(f"【{level}评分标准】")
        print("=" * 60)
        entries = parse_standards(standards_map[level], level=level)
        all_entries.extend(entries)
        print(f"共 {len(entries)} 条")
        for e in entries:
            print(f"\n--- {e.triple} (总分上限 {e.total_max}) ---")
            for d in e.dimensions:
                print(f"  [{d.name}] ({d.max_score}分)")
            if e.special_notes:
                print(f"  特别说明: {e.special_notes[:200]}...")
            if e.syntax_penalty_rule:
                print(f"  语法扣分: {e.syntax_penalty_rule}")

    # 解析参考代码
    print("\n" + "=" * 60)
    print("【参考代码】")
    print("=" * 60)
    refs = parse_reference_codes(ref_path)
    print(f"共 {len(refs)} 段")
    for triple, code in sorted(
        refs.items(),
        key=lambda kv: (level_sort_key(kv[0].level), kv[0].session, kv[0].question),
    ):
        print(f"\n--- {triple} ---")
        head = "\n".join(code.splitlines()[:8])
        print(head)
        if len(code.splitlines()) > 8:
            print(f"  ... (共 {len(code.splitlines())} 行)")

    # 校验：standards 与 reference_codes key 集合
    print("\n" + "=" * 60)
    print("【一致性校验】")
    print("=" * 60)
    std_keys = {e.triple for e in all_entries}
    ref_keys = set(refs.keys())
    only_std = std_keys - ref_keys
    only_ref = ref_keys - std_keys
    print(f"标准库三元组: {len(std_keys)} 个")
    print(f"参考代码三元组: {len(ref_keys)} 个")
    if only_std:
        print(f"⚠ 仅在标准库中（缺参考代码）: {sorted(str(t) for t in only_std)}")
    if only_ref:
        print(f"⚠ 仅在参考代码中（缺评分标准）: {sorted(str(t) for t in only_ref)}")
    if not only_std and not only_ref:
        print("✅ key 集合一致")

    return 0


if __name__ == "__main__":
    sys.exit(main())
