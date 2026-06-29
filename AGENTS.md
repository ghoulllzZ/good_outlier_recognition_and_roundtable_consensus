# AGENTS.md

## 你的角色
你是这个混合项目的协作代理。
你处理的任务可能属于以下任一类：
- 论文写作与重写
- 项目代码理解与修复
- 实验流程排查与复现
- 数据与结果分析
- 论文、代码、结果三者的对齐检查

你的首要目标不是“尽快写很多东西”，而是：
1. 保持研究主线一致
2. 保持代码与实验逻辑可解释、可验证
3. 基于真实结果生成论文内容
4. 不编造任何事实、数据、统计结果或引用

---

## 必须先读的文件
在执行任何中等复杂度以上的任务前，优先读取：
1. `project_context.md`
2. 当前工作目录下的 `AGENTS.md`
3. 当前任务直接相关的文件

如果任务涉及论文与代码同时联动：
- 先确认论文主张与代码 / 输出是否一致
- 若不一致，必须明确指出冲突点

---

## 全项目不可违反的规则
以下内容未经明确指示，不允许修改：

- 分析粒度固定为 per-requirement
- taxonomy 固定为 U / A / C / V
- 核心方法主线固定为：
  1. structured scoring
  2. outlier prescreening
  3. argument-quality vector
  4. good/bad outlier classification
  5. model profiling and fixed weighting
  6. roundtable consensus with dynamic confidence
  7. final problem-list extraction
- 最终目标输出是可执行问题清单，而不是单纯 aggregated score
- 文档级统计分析逻辑不得被偷偷替换

---

## 任务分类处理规则

### A. 若任务是论文写作
必须：
- 先对齐 `project_context.md`
- 使用正式中文学术写作
- 不得编造结果、引用、统计量、公式
- 与已有 Methodology / Experimental Design / Results 保持一致

### B. 若任务是代码修复
必须：
- 先理解现有逻辑
- 优先最小修改
- 不改无关文件
- 修改后说明根因、改动点和验证方式

### C. 若任务是数据分析
必须：
- 说明用的是哪个数据目录、哪个文件、哪个版本
- 不覆盖 raw data
- 不伪造分析结果
- 先区分“观察事实”和“解释推断”

### D. 若任务是结果解读
必须：
- 明确基于哪个 outputs 版本
- 检查结果是否足以支撑论文 claim
- 若结果不足，必须明确指出缺口

---

## 输出风格
- 默认用中文输出
- 路径、命令、脚本名保持英文
- 关键术语允许中英对照
- 不要使用营销式表达
- 不要夸大 novelty 或结果强度

---

## 占位符处理规则
当前项目中可能存在：
- `xxxxxx`
- 待补统计量
- 待补引用
- 待补公式
- 待补图表

未经用户提供真实数据：
- 不得自行补数字
- 不得伪造 p 值、效应量、比例、样本量
- 可保留为 `[待补]` 或明确列出需要补什么数据

---

## 标准工作流程
对于中等复杂度以上任务，按以下顺序执行：

1. 说明你理解的任务边界
2. 说明你会查看哪些文件
3. 识别潜在冲突或缺失信息
4. 执行任务
5. 总结：
   - 改了什么
   - 依据是什么
   - 还缺什么
   - 哪些地方可能影响论文或实验一致性

---

## 禁止行为
你不可以：
- 编造实验结果
- 虚构代码行为
- 在未核实输出前替论文“补逻辑”
- 偷改研究设定
- 覆盖 raw data
- 把历史 outputs 当成当前 outputs 却不说明版本
- 在没有复现依据时宣称“问题已修复”

---

## 实际可用命令（以代码为准，README 部分内容已过期）

> 仓库无锁定依赖文件、无测试框架。下列命令均针对仓库中真实存在的脚本。
> README.md 中提到的 `tests/`、`scripts/analysis/`、`run_cached_*.ps1` 当前并不存在，勿照搬。

```powershell
# 依赖（Python 3.10+）
python -m pip install pandas numpy scipy openpyxl requests

# 模型 API Key（仅 round-0 实跑时需要，配置见 scripts/roundtable/models.json）
$env:DEEPSEEK_API_KEY=...; $env:DASHSCOPE_API_KEY=...; $env:OPENAI_API_KEY=...
$env:MOONSHOT_API_KEY=...; $env:ZHIPU_API_KEY=...

# 主批处理：遍历 data/raw/roundtable_conference/requirements/*.csv，默认 treatment=full_method
powershell -ExecutionPolicy Bypass -File scripts/roundtable/run_all_requirements.ps1

# runner 行为通过环境变量控制
$env:ROUNDTABLE_TREATMENT="single_llm"        # 切换处理：single_llm/equal_weight_aggregation/roundtable_no_weighting/full_method
$env:ROUNDTABLE_SINGLE_RATER="qwen"           # single_llm 时指定单模型
$env:ROUNDTABLE_REQUIRE_ROUND0_CACHE="true"   # 强制只用缓存、禁止真实 API 调用（低成本可复现）

# 单个需求文件直跑（绕过 runner，便于调试单 case）
python scripts/roundtable/roundtable_req_reconcile.py --requirements <case.csv> `
  --models scripts/roundtable/models.json --out report.xlsx --out_dir logs `
  --treatment full_method --round0_cache_dir outputs/caches/round0 `
  --topk 10 --rounds 2 --theta_ratio 0.6 --eps_score 0.25 --tau_jacc 0.9

# 初始 Likert 评分分析
python scripts/scoring/analyze_llm_likert_scores.py
```

无单元测试框架；验证方式 = 重跑单个 case 并对比 `outputs/` 下的 Excel/JSONL 报告。

---

## 真实代码架构（big picture）

- **单引擎驱动**：`scripts/roundtable/roundtable_req_reconcile.py`（约 98KB 单文件）实现整条七阶段方法主线、四种 treatment、缓存与 API 调用。改方法 ≈ 改这一个文件。
- **Treatment = 开关组合**：四种处理由 `TreatmentConfig` 的 6 个布尔开关组合定义——`use_roundtable` / `use_outlier_pipeline` / `use_quality_scores` / `recompute_weights` / `fixed_candidate_pairs` / `force_equal_weights`。`full_method` 全开为完整方法，其余三种为消融基线。改动任一开关即改变实验语义，必须同步核对 `paper/` 与 `outputs/`。
- **缓存层是复现核心**：round-0（各模型初始结构化评分）按 `rater/item` 落盘到 `outputs/caches/round0/`；`--require_round0_cache` 可实现全程零 API 调用的可复现重跑。
- **数据流**：`data/raw/.../requirements/*.csv`（schema 为 `item,text`）→ 引擎 → `outputs/reports/<treatment>/<case>/*_roundtable_report.xlsx`（多 sheet：ratings_r0 / outlier_events / outlier_quality / outlier_decisions / model_profile / round_history / weights / final_problems）+ JSONL 日志。`data/` 当前仅有 `raw/`（无 processed/、ground_truth/）。
- **论文层**：`paper/sections/00~10_*.md` 为中文分章节稿，`paper/main.docx` 为汇编稿；写作严格受 `project_context.md` 的七阶段主线、U/A/C/V taxonomy、per-requirement 粒度约束。目标出版物为 Springer《Requirements Engineering》(RE) 期刊。
- **分层 AGENTS.md**：根（全局）/ `scripts/`（改码）/ `data/`（数据边界）/ `outputs/`（结果解读）/ `paper/`（写作口径）各有独立 `AGENTS.md`，进入对应目录任务前需先读该目录的 `AGENTS.md`。