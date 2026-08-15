# 学园阅卷大师

基于 LLM 的自动化阅卷工具（CLI 批处理）。自动完成「抓取答卷 → 识别这是哪道题 → 按评分标准评分 → 回写 xlsx」，仅对低置信度的行保留人工复核。

> 架构：编排器控制流 + LLM 结构化输出。LLM 作为"评分函数"被调用，不涉及 ReAct agent 的自主决策循环。

## 快速上手

### 1. 环境准备

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                 # 以可编辑模式安装本包（否则 python -m grading_agent.cli 会报 ModuleNotFoundError）
playwright install chromium      # 必需：装 Chromium 引擎
copy .env.example .env           # 编辑 .env 填入 DeepSeek API key
```

> 代码位于 `src/grading_agent/` 下，`pip install -e .` 以可编辑模式把它装进当前环境，
> 之后 `python -m grading_agent.cli` 才能找到模块；且后续改代码即时生效、无需重装。

### 2. 准备任务文件夹

每次阅卷在一个独立文件夹中进行，内含：

```txt
tasks/7.12/
├── 阅卷记录表-0712.xlsx   # 输入（含链接、题目ID）+ 输出（回写评分列）
├── 一级评分标准.docx
├── 二级评分标准.docx
└── 7月参考代码.docx
```

### 3. 正式阅卷（回写 xlsx）

```bash
python -m grading_agent.cli "tasks/7.12" --provider deepseek --model deepseek-v4-flash --confidence-threshold 0.7
```

### 4. 一致性验证（不写回 xlsx，对比人工评分）

```bash
python -m grading_agent.cli "tasks/7.12" --verify --provider deepseek
# 或便捷入口：
python scripts/run_verify.py tasks/7.12
# prompt 调优后回归（跳过已识别的题目ID）：
python scripts/run_verify.py tasks/7.12 --skip-identification
```

## 命令行参数

| 参数 | 默认 | 说明 |
| ------ | ------ | ------ |
| `task_dir` | （必填） | 任务文件夹路径 |
| `--provider` | deepseek | LLM 提供方：deepseek/openai/claude |
| `--model` | deepseek-chat | LLM 模型名 |
| `--confidence-threshold` | 0.7 | 高/低置信度分流阈值 |
| `--max-rows` | 全部 | 调试：只跑前 N 行 |
| `--debug-fetch` | 关 | 每次抓取都写 render_dumps |
| `--skip-identification` | 关 | 跳过题目识别，从已有 jsonl 恢复映射 |
| `--force-rerun` | 关 | 忽略已完成行，全部重新评分（默认会断点续跑） |
| `--concurrency` | 8 | 阶段 C 并发 worker 数（1=串行，等价旧行为；建议 4-8） |
| `--verify` | 关 | 一致性验证模式（不写回 xlsx） |

## 工作流（阶段 A-D）

- **阶段 A**：读 xlsx 收集题目ID + 解析 3 个 docx 构建标准库 + 冒烟测试连通性
- **阶段 B**：每个新题目ID 抓首行 → LLM 1-of-N 分类 → CLI 逐个确认 → 进程内缓存
- **阶段 C**：并发遍历每行（`--concurrency` 个 worker）→ 抓取 → 缓存命中 → LLM 评分 → 高置信度写回/低置信度留空
- **阶段 D**：保存 xlsx + 导出待复核清单 + 评分明细日志

## 调试脚本

```bash
# 单 URL 抓取验证
python scripts/fetch_one.py "<URL>" --debug

# docx 解析验证（打印标准 + 参考代码）
python scripts/parse_docx_debug.py tasks/7.12
```

## 输出产物（`output/<任务名>/`）

- `<原名>_AI评分.xlsx`：**AI 评分与人工改分的写入目标**（首次跑批时从原始 xlsx 复制）
- `grading_detail.jsonl`：每行评分明细（维度分项+扣分+置信度+理由+题目描述+学生代码），断点续跑的数据源
- `human_override.jsonl`：人工改分流水（可追溯、可撤销）
- `review_queue.csv`：待复核清单（低置信度行 + 抓取失败行）
- `verification_report.txt`：一致性验证报告
- `run.log`：跑批日志（GUI 跑批时落盘，追加模式）
- `render_dumps/`：抓取调试转储（默认仅失败时写）

> **`tasks/<任务名>/` 下的原始 xlsx 全程只读**，不会被写入。它含真人评分，
> 既是交付基准，也是一致性验证的对比来源——写入会让 AI 分覆盖人工分、偏差恒为 0。

## 关键设计

- xlsx 按表头名定位列（兼容 7列/12列布局），禁止固定索引
- 评分标准解析自适应场次（一级复制成4条，二级检测上午/下午场标题）
- 题目识别缓存仅进程内（题目ID 跨月不稳定）
- temperature=0，评分可复现
- 验证模式 `skip_score=True`，从数据结构层面保证工具不看 G 列
- AI 评分写 output 下的独立副本，原始 xlsx 只读
- 阶段 C 多 worker 并发（每 worker 独立浏览器 + LLM 客户端，主线程统一落盘），`--concurrency` 可调
- 默认 DeepSeek（成本低），Claude/OpenAI 可切换

详见 `prd.md`。
