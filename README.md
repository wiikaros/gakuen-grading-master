# 学园阅卷大师

针对特定阅卷任务设计的基于 LLM 的自动化阅卷工具，自动完成「抓取答卷 → 识别题目 → 按评分标准评分 → 回写 xlsx」，仅对低置信度的行保留人工复核。

提供两种使用形态：

- **CLI 批处理**：`python -m grading_agent.cli`，适合整批任务自动化评分与一致性验证。
- **GUI 图形界面**：`python scripts/gui.py`，含「跑批监控台」（实时进度、确认弹窗）与「结果复核」（浏览/改分/单行重评）。

> 架构：编排器控制流 + LLM 结构化输出。LLM 作为「评分函数」被调用，不涉及 ReAct agent 的自主决策循环。

---

## 目录

1. [项目简介](#1-项目简介)
2. [使用前的准备](#2-使用前的准备)
3. [如何使用](#3-如何使用)
4. [参数和配置说明](#4-参数和配置说明)
5. [常见问题](#5-常见问题)
6. [目录结构](#6-目录结构)

---

## 1. 项目简介

### 1.1 这是什么

阅卷平台的阅卷工作原本由人工逐份打开答卷 URL、对照标准答案与评分标准打分、回填 xlsx。一次任务动辄数百行，耗时且易疲劳出错。

本工具把这一过程自动化：读取任务文件夹里的 `xlsx`（答卷链接清单）和若干 `docx`（评分标准 + 参考代码），用 Playwright 抓取每份答卷的学生代码，用 LLM 识别题目并逐维度评分，把高置信度的分数回写 xlsx，低置信度的行导出待复核清单供人工处理。

### 1.2 核心功能

| 功能 | 说明 |
| --- | --- |
| 答卷抓取 | Playwright 抓取 URL，提取「题目描述 + 学生代码」（兼容 iframe 渲染页） |
| 题目识别 | LLM 在任务全部参考题里做 1-of-N 分类，输出候选编号 + 置信度 |
| 自动评分 | 逐维度打分 + 语法扣分（A-F 封闭清单），输出总分 + 理由 + 置信度 |
| 置信度分流 | 高置信度直接回写 xlsx；低置信度留空并进待复核清单 |
| 断点续跑 | 每行评分即写 JSONL，中断后可跳过已完成行继续 |
| 一致性验证 | 对照人工评分，输出 ±2 分一致率等指标（不写回 xlsx） |
| 多模型切换 | DeepSeek（默认，成本低）/ OpenAI / Claude |
| 并发加速 | 阶段 C 多 worker 并发抓取+评分（`--concurrency` 可调） |
| 通用级别 | 评分标准与参考代码按「级别 × 场次 × 题号」组织，支持一级/二级/三级…任意级别 |

### 1.3 工作原理（阶段 A–D）

一次完整跑批分四个阶段：

- **阶段 A — 启动准备**：读取 xlsx 收集题目 ID；解析任务文件夹内所有评分标准 docx + 参考代码 docx 构建标准库；冒烟测试预抓取验证连通性。
- **阶段 B — 题目识别**：对每个新题目 ID，抓取其首行 URL，LLM 做 1-of-N 分类，用户确认（`y` 采纳推荐 / `n` 拒绝 / 数字选其他候选）后缓存「题目 ID → 三元组」映射。
- **阶段 C — 批量评分**：并发遍历每行（`--concurrency` 个 worker），抓取 → 命中缓存 → LLM 评分 → 高置信度写回 / 低置信度留空。
- **阶段 D — 收尾**：保存 xlsx、导出待复核清单与评分明细日志。

### 1.4 评分模型

评分是「逐维度 + 语法扣分」两层结构：

- **维度分**：从评分标准 docx 动态解析（典型 5 个维度，每维度满分 5 分；也支持 4 条维度、其中一条满分 10 分等非规整情况，维度数与满分均由 docx 决定）。
- **语法扣分**：封闭的 A-F 六类清单（拼写错误 / 分号问题 / 括号引号不匹配 / 未定义标识符 / 语句结构破损 / 中英文标点混用），每类无论出现几次只扣 1 分，总扣分最多 6 分。
- **总分** = `max(0, min(总分上限, 维度合计 − 语法扣分))`，其中「总分上限」为该题各维度满分之和。

### 1.5 技术栈

Python 3.10+ · LangChain（多模型抽象 + 结构化输出）· Playwright（页面抓取）· openpyxl（xlsx 读写）· python-docx（标准库解析）· Pydantic（输出 schema）· tkinter（GUI）。

---

## 2. 使用前的准备

### 2.1 环境要求

- Python **3.10 或更高版本**（本项目在 3.12 上验证通过）
- 可访问外网（抓取答卷 URL、调用 LLM API）
- 一个可用的 LLM API key（DeepSeek / OpenAI / Claude 任选其一）

### 2.2 安装步骤

```bash
# 1. 创建并激活虚拟环境（推荐）
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 以可编辑模式安装本包（必需）
#    代码位于 src/grading_agent/ 下，不装这一步，python -m grading_agent.cli 会报 ModuleNotFoundError
pip install -e .

# 4. 安装 Chromium 引擎（必需，Playwright 抓取页面用）
playwright install chromium

# 5. 配置环境变量（见下节）
copy .env.example .env        # Windows；Linux/macOS 用 cp .env.example .env
```

> `pip install -e .` 以可编辑模式安装，之后修改 `src/` 下的代码即时生效、无需重装。换新环境时需重新执行一次。

### 2.3 配置文件

项目有两层配置，**优先级从高到低**：

```
命令行参数  >  环境变量(.env)  >  config/default.yaml  >  代码内置默认值
```

**① `.env` 文件**（放 API key，以及可选的环境变量覆盖项）

复制 `.env.example` 为 `.env`，填入你的 API key：

```bash
DEEPSEEK_API_KEY=sk-xxxxxxxx            # DeepSeek key（用 deepseek 时必需）
DEEPSEEK_BASE_URL=https://api.deepseek.com
OPENAI_API_KEY=sk-xxxxxxxx              # 可选
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx       # 可选
```

**② `config/default.yaml` 文件**（各配置项的默认值，详见 [§4.3](#43-configdefaultyaml-配置项)）

### 2.4 准备任务文件夹

每次阅卷在一个独立文件夹中进行，内含：

```text
tasks/task_name/
├── 阅卷记录表.xlsx            # 输入（含题目链接、题目ID）+ 输出（回写评分列）
├── 一级评分标准.docx          # 该级别的评分标准（维度 + 满分）
├── 二级评分标准.docx          # 支持任意级别，如再放「三级评分标准.docx」即可
└── 参考代码.docx             # 标准答案代码（文件名需含「参考代码」）
```

约定：

- **xlsx**：任务文件夹中唯一的 `.xlsx` 文件（排除 `~$` 临时文件和 `_autosave` 文件）。必须包含「题目链接」「题目ID」等列（按表头名匹配，兼容 7 列/12 列布局）。
- **评分标准 docx**：文件名形如 `<级别>评分标准.docx`（如 `一级评分标准.docx`、`三级评分标准.docx`）。工具会自动扫描识别任务文件夹里所有这样的文件，级别数量不限。
- **参考代码 docx**：文件名含「参考代码」。内部按「级别 × 场次（上午/下午）× 题号」组织，题数不限，解析器自动识别。
- **原始 xlsx 全程只读**：AI 评分写入 `output/<任务名>/` 下的独立副本，不覆盖原始文件（原始文件里的真人评分是一致性验证的对比基准）。

---

## 3. 如何使用

### 3.1 CLI 命令行

#### 正式阅卷（回写 xlsx）

```bash
python -m grading_agent.cli "tasks/task_name"
```

默认使用 `config/default.yaml` 里的模型与阈值（当前为 DeepSeek + `deepseek-v4-flash` + 阈值 0.8 + 并发 4）。

指定参数覆盖默认值：

```bash
python -m grading_agent.cli "tasks/task_name" \
    --provider deepseek \
    --model deepseek-v4-flash \
    --confidence-threshold 0.8 \
    --concurrency 4
```

#### 一致性验证（不写回 xlsx，对比人工评分）

```bash
# 注意，此一致性检验会调用 LLM 重新评分，产生额外费用
python -m grading_agent.cli "tasks/task_name" --verify
# 或便捷入口：
python scripts/run_verify.py tasks/task_name
# prompt 调优后回归（跳过已识别的题目ID，只重评）：
python scripts/run_verify.py tasks/task_name --skip-identification

# 如果已有 grading_detail.jsonl，可直接用它算一致率（不再抓取/识别/评分、不产生 LLM 费用）：
python scripts/compute_accuracy_from_normal_run.py tasks/task_name
```

#### 调试：只跑前 N 行

```bash
python -m grading_agent.cli tasks/task_name --max-rows 5 --debug-fetch
```

### 3.2 GUI 图形界面

```bash
python scripts/gui.py
```

两个页签：

- **跑批监控台**：选择任务文件夹 → 预检 → 跑批（实时进度/日志、阶段 B 确认弹窗、停止）。参数区可调模型、置信度阈值、并发数、只跑前 N 行，以及验证模式 / 跳过识别 / 每次抓取写转储 / 强制重跑等选项。
- **结果复核**：浏览历史评分结果、按状态/置信度筛选、逐行查看详情、人工改分回写、单行重评。

> 浏览历史结果不需要 API key、不花钱；只有跑批与重评会调用 LLM。

### 3.3 调试脚本

```bash
# 单 URL 抓取验证（确认能抓到题目描述和代码）
python scripts/fetch_one.py "<答卷URL>"
python scripts/fetch_one.py "<答卷URL>" --debug      # 额外写 render_dumps 转储

# docx 解析验证（打印各级评分标准 + 参考代码 + 一致性校验）
python scripts/parse_docx_debug.py tasks/task_name
```

> 新增任务前，强烈建议先跑 `parse_docx_debug.py` 确认评分标准与参考代码解析正确、key 集合一致，再跑批。

### 3.4 常见操作示例

#### 断点续跑

跑批中断（Ctrl+C 或异常退出）后，直接重新运行同一条命令即可——已完成的行已逐行写入 JSONL，会自动跳过，只处理剩余行：

```bash
python -m grading_agent.cli "tasks_name"
```

#### 强制重跑（忽略历史结果，全部重新评分）

```bash
python -m grading_agent.cli "tasks_name" --force-rerun
```

> 注意：会重新调用 LLM 评分已完成的全部行，产生额外费用。

#### 调并发数

```bash
# 8 并发（更快，但更考验网络/API 限流）
python -m grading_agent.cli "tasks_name" --concurrency 8
# 1 = 串行（最稳，等价旧行为）
python -m grading_agent.cli "tasks_name" --concurrency 1
```

### 3.5 输出产物（`output/<任务名>/`）

| 文件 | 说明 |
| --- | --- |
| `<原名>_AI评分.xlsx` | AI 评分与人工改分的写入目标（首次跑批时从原始 xlsx 复制） |
| `grading_detail.jsonl` | 每行评分明细（维度分项 + 扣分 + 置信度 + 理由 + 题目描述 + 学生代码），断点续跑的数据源 |
| `human_override.jsonl` | 人工改分流水（可追溯、可撤销） |
| `review_queue.csv` | 待复核清单（低置信度行 + 抓取失败行） |
| `verification_report.txt` | 一致性验证报告（`--verify` 模式或 `compute_accuracy_from_normal_run.py` 生成） |
| `run.log` | 跑批日志（GUI 跑批时落盘，追加模式） |
| `render_dumps/` | 抓取调试转储（默认仅失败时写） |

---

## 4. 参数和配置说明

### 4.1 配置优先级（重要）

配置按以下顺序生效，**后者覆盖前者**：

1. 代码内置默认值（`src/grading_agent/config.py` 中的 dataclass 默认值）
2. `config/default.yaml`
3. 环境变量（`.env` 及 shell 环境中的 `GRADING_*` / API key 变量）
4. 命令行参数（`--provider` / `--model` / `--confidence-threshold` / `--concurrency`）

> **在 `.env` 中出现的变量会覆盖 yaml 的同名配置。** 若改了 yaml 不生效，先查 `.env`。

### 4.2 命令行参数

| 参数 | 默认 | 取值范围 | 说明 |
| --- | --- | --- | --- |
| `task_dir` | （必填） | 目录路径 | 任务文件夹路径（含 xlsx + 评分标准 docx + 参考代码 docx） |
| `--provider` | 从 config 读 | `deepseek` / `openai` / `claude` | LLM 提供方 |
| `--model` | 从 config 读 | 模型名 | LLM 模型名（如 `deepseek-v4-flash`） |
| `--confidence-threshold` | 从 config 读 | `0.0`–`1.0` | 高/低置信度分流阈值；低于此值的评分不写回、进待复核清单 |
| `--max-rows` | 全部 | 正整数 | 调试用：只处理前 N 行 |
| `--debug-fetch` | 关 | 开关 | 每次抓取都写 render_dumps 转储（默认仅失败时写） |
| `--skip-identification` | 关 | 开关 | 跳过阶段 B 题目识别，从已有 JSONL 恢复「题目 ID → 三元组」映射 |
| `--force-rerun` | 关 | 开关 | 忽略已完成行，全部重新评分（默认会断点续跑跳过已评分行） |
| `--concurrency` | 从 config 读 | 正整数 | 阶段 C 并发 worker 数；`1` = 串行（等价旧行为），建议 4–8 |
| `--verify` | 关 | 开关 | 一致性验证模式：不写回 xlsx，跑完对照人工评分输出报告 |

> `--provider` / `--model` / `--confidence-threshold` / `--concurrency` 的默认值标注为「从 config 读」，即不传时使用 yaml（或 `.env` 覆盖）里的值，传了才覆盖。

### 4.3 `config/default.yaml` 配置项

```yaml
playwright:
  headless: true                    # 无头模式（true=不弹浏览器窗口）
  timeout_ms: 20000                 # 页面加载超时（毫秒）
  wait_until: networkidle           # 等待策略：networkidle / domcontentloaded

llm:
  provider: deepseek                # deepseek / openai / claude
  model: deepseek-v4-flash          # 模型名
  temperature: 0.0                  # 固定 0，保证评分可复现，勿改
  max_retries: 3                    # LLM 调用失败重试次数

grading:
  confidence_threshold: 0.8         # 高/低置信度分流阈值
  write_back: true                  # 是否回写 xlsx（verify 模式会被强制关闭）
  max_score: 25                     # GUI 人工改分范围上限（总分上限实际由各题维度满分之和动态决定）
  xlsx_save_interval: 50            # 每 N 行持久化一次 xlsx（断点续跑间隔）
  concurrency: 4                    # 阶段 C 并发 worker 数；1=串行；建议 4-8

circuit_breaker:
  max_consecutive_fetch_failures: 10  # 连续抓取失败超过此数触发熔断
  max_consecutive_llm_failures: 5     # 连续 LLM 调用失败超过此数触发熔断
  action: prompt                       # 熔断后动作：prompt=询问用户 / skip=自动跳过 / abort=直接退出

output:
  detail_log: grading_detail.jsonl
  review_queue: review_queue.csv
  render_dumps_subdir: render_dumps
  render_dumps_on_failure_only: true   # true=仅失败写转储；false=每次抓取都写
```

各配置项说明：

| 配置项 | 含义 | 默认值 | 修改建议 |
| --- | --- | --- | --- |
| `playwright.headless` | 是否无头模式 | `true` | 调试抓取时可临时改 `false` 看浏览器窗口 |
| `playwright.timeout_ms` | 页面加载超时（毫秒） | `20000` | 网络慢可调大；但过大拖慢整体 |
| `playwright.wait_until` | 页面等待策略 | `networkidle` | 一般不改；页面加载慢时可试 `domcontentloaded` |
| `llm.provider` | LLM 提供方 | `deepseek` | 三选一，需配对应 API key |
| `llm.model` | 模型名 | `deepseek-v4-flash` | 按所选 provider 填对应模型名 |
| `llm.temperature` | 采样温度 | `0.0` | 固定 0 保证可复现，**不要改** |
| `llm.max_retries` | LLM 重试次数 | `3` | 网络不稳可调大 |
| `grading.confidence_threshold` | 置信度阈值 | `0.8` | 调高→更多行进人工复核、更保守；调低→更多行自动写回 |
| `grading.write_back` | 是否回写 xlsx | `true` | 一般不改；`--verify` 会自动关 |
| `grading.max_score` | GUI 改分范围上限 | `25` | 若题目总分上限非 25，需同步改（否则 GUI 改分校验不符） |
| `grading.xlsx_save_interval` | 定期保存间隔（行） | `50` | 断点续跑粒度，越小越不易丢但越慢 |
| `grading.concurrency` | 并发 worker 数 | `4` | 建议 4–8；过高可能触发 API 限流 |
| `circuit_breaker.*` | 熔断阈值与动作 | 见上表 | 一般不改 |
| `output.*` | 输出文件名与转储策略 | 见上表 | 一般不改 |

### 4.4 环境变量（`.env`）

| 变量 | 含义 | 必需 | 默认/说明 |
| --- | --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API key | 用 deepseek 时必需 | 无 |
| `DEEPSEEK_BASE_URL` | DeepSeek 接口地址 | 否 | `https://api.deepseek.com` |
| `OPENAI_API_KEY` | OpenAI API key | 用 openai 时必需 | 无 |
| `ANTHROPIC_API_KEY` | Anthropic API key | 用 claude 时必需 | 无 |
| `GRADING_MODEL_PROVIDER` | 覆盖 `llm.provider` | 否 | 覆盖 yaml |
| `GRADING_MODEL_NAME` | 覆盖 `llm.model` | 否 | 覆盖 yaml |
| `GRADING_CONFIDENCE_THRESHOLD` | 覆盖 `grading.confidence_threshold` | 否 | 覆盖 yaml |
| `GRADING_PLAYWRIGHT_TIMEOUT_MS` | 覆盖 `playwright.timeout_ms` | 否 | 覆盖 yaml |
| `GRADING_CONCURRENCY` | 覆盖 `grading.concurrency` | 否 | 覆盖 yaml |
| `GRADING_DEBUG_FETCH` | 覆盖转储策略 | 否 | **注意语义反向**：`false` → 仅失败写转储；`true` → 每次抓取都写 |

> **`GRADING_DEBUG_FETCH` 语义提醒**：它映射到 yaml 的 `render_dumps_on_failure_only`，但取值方向相反——
> `GRADING_DEBUG_FETCH=false` 等价于 `render_dumps_on_failure_only: true`（仅失败写转储），
> `GRADING_DEBUG_FETCH=true` 等价于 `render_dumps_on_failure_only: false`（每次写）。

---

## 5. 常见问题

**Q：运行 `python -m grading_agent.cli` 报 `ModuleNotFoundError: No module named 'grading_agent'`？**

A：代码在 `src/grading_agent/` 下，需要先在项目根目录下执行 `pip install -e .` 以可编辑模式安装本包。装一次即可，之后改代码即时生效。

**Q：改了 `config/default.yaml` 里的配置，但运行结果还是旧值？**

A：检查 `.env` 文件里是否有对应的变量覆盖了它。环境变量优先级高于 yaml。要么两处都改，要么删掉 `.env` 里的对应行让它回退到 yaml 的值。

**Q：参考代码 docx 解析结果为 0 段？**

A：可能是 docx 的标题格式问题。解析器已兼容两种格式：「周六上午场参考代码：」+「一级：」这种场次/级别分开的格式，以及「一级（上午）：」这种级别+场次合并的格式。若仍解析为 0 段，请用 `python scripts/parse_docx_debug.py <任务文件夹>` 查看实际解析情况，并检查 docx 里级别/场次标题的写法。

**Q：评分标准里某维度满分不是 5 分（如 10 分），能正常处理吗？**

A：能。维度数与各维度满分均从 docx 动态解析，总分上限也动态计算。但注意 `config/default.yaml` 里 `grading.max_score`（GUI 改分范围上限）默认是 25，若题目总分上限非 25，建议同步修改，否则 GUI 人工改分的范围校验会与实际不符。

**Q：支持超过「一级/二级」的级别吗（如三级）？**

A：支持。评分标准 docx 按 `<级别>评分标准.docx` 命名即可，级别数量不限（一级/二级/三级/四级…），参考代码 docx 里的题数也不限。工具会自动扫描识别。

**Q：跑批中断了怎么办？**

A：已完成的行已逐行写入 `grading_detail.jsonl`，直接重新运行同一条命令即可断点续跑，不会重复处理已完成行。

**Q：跑批时提示「未配置 API key」？**

A：检查 `.env` 里是否填了对应 provider 的 API key（如 `DEEPSEEK_API_KEY`），且 provider 与 key 对应。

---

## 6. 目录结构

```text
学园阅卷大师/
├── README.md                    # 本文档
├── prd.md                       # 产品需求文档（设计细节）
├── requirements.txt             # Python 依赖清单
├── pyproject.toml               # 打包配置（支持 pip install -e .）
├── .env.example                 # 环境变量模板（复制为 .env 使用）
├── config/
│   └── default.yaml             # 默认配置（模型/阈值/并发/超时等）
├── src/
│   └── grading_agent/
│       ├── cli.py               # CLI 入口（argparse + 阶段 A-D 编排）
│       ├── config.py            # 配置加载（yaml + 环境变量覆盖）
│       ├── models.py            # 数据结构 + 级别工具函数
│       ├── standard_library.py  # 标准库构建与索引（自动发现 docx）
│       ├── docx_parser.py       # 评分标准 docx 解析
│       ├── reference_codes.py   # 参考代码 docx 解析（状态机）
│       ├── xlsx_io.py           # xlsx 读写（按表头名定位列）
│       ├── url_fetcher.py       # Playwright 抓取工具
│       ├── logging_utils.py     # JSONL 明细日志 + 待复核清单
│       ├── llm/                 # LLM 封装
│       │   ├── base.py          # 抽象接口
│       │   ├── langchain_backend.py  # LangChain 实现 + 模型工厂
│       │   └── prompts.py       # 识别 prompt + 评分 prompt
│       ├── agent/               # 编排
│       │   ├── orchestrator.py  # 主编排（阶段 A-D）
│       │   ├── identifier.py    # 题目识别
│       │   └── grader.py        # 评分
│       ├── gui/                 # GUI（跑批监控台 + 结果复核）
│       └── verify/
│           └── consistency.py   # 一致性验证
├── scripts/                     # 便捷脚本
│   ├── gui.py                   # GUI 启动入口
│   ├── fetch_one.py             # 单 URL 抓取调试
│   ├── parse_docx_debug.py      # docx 解析调试
│   ├── run_verify.py            # 一致性验证便捷入口
│   └── compute_accuracy_from_normal_run.py  # 基于普通模式跑批产物直接算一致率（不重跑评分）
├── tests/                       # pytest 测试套件
├── tasks/                       # 任务文件夹（gitignore），用户放置
└── output/                      # 运行产物（gitignore），按任务名分目录
```

> `tasks/`、`output/`、`.env` 均被 `.gitignore` 排除（含学生个人信息、答卷内容与 API key，切勿放宽提交规则）。
