# Overleaf 项目说明（Springer RE 期刊草稿）

本目录是论文 *Beyond Majority Vote: Improving Per-Requirement Quality Assessment via Good Outlier Recognition and Roundtable Consensus* 的 Overleaf / LaTeX 版本，依据 `paper/main.docx` 当前草稿生成，并按 `outputs/tables/Table Insertion Guide for RE Paper.docx` 的指示插入 5 张表格。

## 目录结构

```
overleaf/
├── main.tex                  # 主文件（ctexart 类 + 中文支持，Overleaf 自带，无需上传 cls）
├── tables/
│   ├── table_01.tex          # Table 1  Dataset and ground-truth summary
│   ├── table_02.tex          # Table 2  Treatments, baselines, ablation
│   ├── table_03.tex          # Table 3  Metrics and statistical tests by RQ
│   ├── table_04.tex          # Table 4  Treatment-level performance results
│   └── table_05.tex          # Table 5  Representative good/bad outlier cases
├── figures/
│   ├── fig1_overall_workflow.png
│   ├── fig2_rq1_disagreement.png
│   ├── fig3_outlier_separation.png
│   ├── fig4_convergence.png
│   └── fig5_dimension_shift.png
└── README_OVERLEAF.md
```

## 在 Overleaf 上使用

1. 将整个 `overleaf/` 文件夹打包成 zip 上传到 Overleaf（New Project → Upload Project），或把内容拷入新项目。
2. **编译器必须设为 XeLaTeX**：Overleaf 左上角 `Menu → Compiler → XeLaTeX`。
   - 原因：正文为中文，使用 `xeCJK` + Fandol 字体（TeX Live / Overleaf 自带），只有 XeLaTeX 能正确排版。
3. 主文件设为 `main.tex`，点击 Recompile。

本项目使用 `ctexart` 文档类（Overleaf/TeX Live 自带）+ Fandol 中文字体（自带），**无需上传任何 `.cls` 或字体文件**。

> 注意：Springer 的 `svjour3.cls` 与 `sn-jnl.cls` **并未**预装在 Overleaf，直接用会报
> `File 'svjour3.cls' not found`。因此撰写阶段采用 `ctexart`，投稿前再迁移到官方 Springer 模板（见文末）。

## 表格插入位置（已按指南放置）

| 表格 | 位置（main.tex 章节） |
|------|----------------------|
| Table 1 | 5.2.2 Datasets，PURE/10 文档/978 条/3912 单元描述之后 |
| Table 2 | 5.3 Treatments and Baselines，四种 treatment 介绍之后 |
| Table 3 | 5.6 Data Analysis Methods 末尾 |
| Table 4 | 6.2.2 Treatment-level Performance 开头 |
| Table 5 | 6.2.1 Good and Bad Outlier Separation，计数描述之后 |

图 Figure 1–5 分别置于 Methodology、RQ1、6.2.1、6.2.4、RQ3。

## 需要你补全的地方（已在 main.tex 中以 `% TODO` 标注）

1. **公式**：`main.docx` 中的公式是 Word 公式对象，文本提取无法还原，因此第 4 章（Methodology）多处公式为占位符
   `\text{[公式待补]}`，请对照 `paper/main.docx` 把原始公式逐条填入对应的 `equation` 环境。
   （`score_doc`、离群阈值、好/坏离群阈值、$\kappa$/$\eta$/$\lambda$ 等已按 docx 文字描述写出，可直接核对。）
2. **精确统计量**：RQ1 的 Kruskal--Wallis、RQ2 的 Friedman 等检验，正文只保留了“显著/不显著”的定性结论，
   精确的 $H$、$\chi^2$、$p$、效应量请从 `main.docx` 补全（已标 `% TODO`，当前用 `$p<0.05$ / $p>0.05$` 占位）。
3. **Table 5 占位符**：`<doc--item>`、`<score>/<median>`、需求摘录与模型证据等为占位内容，请填入真实代表性案例。
4. **作者与机构信息**：`\author{}` / `\institute{}` 当前为占位。
5. **参考文献**：`thebibliography` 仅含 26 条题名（来自草稿参考文献表）。投稿前请补全作者、年份、出处等完整信息；
   若改用 BibTeX，可将其转为 `references.bib` 并配 `spbasic` 样式。

## 关于期刊模板

当前使用 `ctexart`，目的是在 Overleaf 上零配置、可直接编译并查看中文正文与表图，适合撰写阶段。
**正式投稿 Springer《Requirements Engineering》前**，请迁移到 Springer Nature 官方模板：
在 Overleaf 模板库搜索 “Springer Nature”，用其新建项目（自带 `sn-jnl.cls`），
再把本项目的 `\section`/正文、`tables/`、`figures/` 与参考文献复制过去，仅需替换文档类与前置元信息
（title/author/abstract/keywords）。若官方英文模板下需要中文，再按需加 `\usepackage{xeCJK}`。
