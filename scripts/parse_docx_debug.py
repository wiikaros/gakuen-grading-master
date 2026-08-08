"""docx 解析结果打印调试脚本。

用法：
    python scripts/parse_docx_debug.py <任务文件夹>

验证阶段2：确认打印 8 条标准 + 8 段参考代码，二级 7.12 上午/下午特别说明确实不同。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from grading_agent.docx_parser import parse_level1_standards, parse_level2_standards
from grading_agent.reference_codes import parse_reference_codes


def find_docx(task_dir: Path, keyword: str) -> Path | None:
    """在任务文件夹里找含关键词的 docx。"""
    for p in task_dir.glob("*.docx"):
        if keyword in p.name:
            return p
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="docx 解析调试")
    parser.add_argument("task_dir", help="任务文件夹路径")
    args = parser.parse_args()
    task_dir = Path(args.task_dir)

    level1_path = find_docx(task_dir, "一级")
    level2_path = find_docx(task_dir, "二级")
    ref_path = find_docx(task_dir, "参考代码")

    if not level1_path:
        print(f"❌ 未在 {task_dir} 找到一级评分标准 docx")
        return 1
    if not level2_path:
        print(f"❌ 未在 {task_dir} 找到二级评分标准 docx")
        return 1
    if not ref_path:
        print(f"❌ 未在 {task_dir} 找到参考代码 docx")
        return 1

    print(f"一级标准: {level1_path.name}")
    print(f"二级标准: {level2_path.name}")
    print(f"参考代码: {ref_path.name}")

    # 解析一级
    print("\n" + "=" * 60)
    print("【一级评分标准】")
    print("=" * 60)
    level1 = parse_level1_standards(level1_path)
    print(f"共 {len(level1)} 条")
    for e in level1:
        print(f"\n--- {e.triple} (总分上限 {e.total_max}) ---")
        for d in e.dimensions:
            print(f"  [{d.name}] ({d.max_score}分)")
        if e.special_notes:
            print(f"  特别说明: {e.special_notes[:120]}...")
        if e.syntax_penalty_rule:
            print(f"  语法扣分: {e.syntax_penalty_rule}")

    # 解析二级
    print("\n" + "=" * 60)
    print("【二级评分标准】")
    print("=" * 60)
    level2 = parse_level2_standards(level2_path)
    print(f"共 {len(level2)} 条")
    for e in level2:
        print(f"\n--- {e.triple} (总分上限 {e.total_max}) ---")
        for d in e.dimensions:
            print(f"  [{d.name}] ({d.max_score}分)")
        if e.special_notes:
            print(f"  特别说明: {e.special_notes[:200]}...")

    # 解析参考代码
    print("\n" + "=" * 60)
    print("【参考代码】")
    print("=" * 60)
    refs = parse_reference_codes(ref_path)
    print(f"共 {len(refs)} 段")
    for triple, code in sorted(refs.items(), key=lambda kv: (kv[0].level, kv[0].session, kv[0].question)):
        print(f"\n--- {triple} ---")
        # 只打印前 8 行
        head = "\n".join(code.splitlines()[:8])
        print(head)
        if len(code.splitlines()) > 8:
            print(f"  ... (共 {len(code.splitlines())} 行)")

    # 校验：standards 与 reference_codes key 集合
    print("\n" + "=" * 60)
    print("【一致性校验】")
    print("=" * 60)
    std_keys = {e.triple for e in level1} | {e.triple for e in level2}
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
