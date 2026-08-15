"""级别工具函数 + 候选编号转换 单元测试。"""
from grading_agent import models
from grading_agent.models import TaskTriple


def test_cn_numeral_to_int():
    assert models.cn_numeral_to_int("一") == 1
    assert models.cn_numeral_to_int("十") == 10
    assert models.cn_numeral_to_int("十一") == 11
    assert models.cn_numeral_to_int("二十") == 20
    assert models.cn_numeral_to_int("二十一") == 21
    assert models.cn_numeral_to_int("3") == 3
    assert models.cn_numeral_to_int("abc") is None
    assert models.cn_numeral_to_int("") is None


def test_extract_level():
    assert models.extract_level("三级：") == "三级"
    assert models.extract_level("3级评分标准") == "三级"
    assert models.extract_level("十二级") == "十二级"
    assert models.extract_level("周六上午场") is None
    assert models.extract_level("一级评分标准.docx") == "一级"


def test_int_to_level():
    assert models.int_to_level(1) == "一级"
    assert models.int_to_level(3) == "三级"
    assert models.int_to_level(10) == "十级"
    assert models.int_to_level(12) == "十二级"
    assert models.int_to_level(20) == "二十级"


def test_level_sort_key():
    levels = ["三级", "一级", "十级", "二级", "四级"]
    assert sorted(levels, key=models.level_sort_key) == ["一级", "二级", "三级", "四级", "十级"]


def test_identification_output_to_result_normal():
    IdentificationOutput, _, _ = models.get_llm_output_models()
    cands = [TaskTriple("一级", "上午", 1), TaskTriple("二级", "上午", 2)]
    r = models.identification_output_to_result(
        IdentificationOutput(candidate_index=2, confidence=0.9, reasoning="x"), cands)
    assert r.parse_ok
    assert r.best_triple == cands[1]
    assert r.confidence == 0.9


def test_identification_output_to_result_out_of_range():
    IdentificationOutput, _, _ = models.get_llm_output_models()
    cands = [TaskTriple("一级", "上午", 1)]
    for bad in (0, 2, 99, -1):
        r = models.identification_output_to_result(
            IdentificationOutput(candidate_index=bad, confidence=0.9, reasoning="x"), cands)
        assert not r.parse_ok
        assert "越界" in r.reasoning


def test_identification_output_to_result_empty_candidates():
    IdentificationOutput, _, _ = models.get_llm_output_models()
    r = models.identification_output_to_result(
        IdentificationOutput(candidate_index=1, confidence=0.9, reasoning="x"), [])
    assert not r.parse_ok
    assert r.best_triple.level == "未定"
