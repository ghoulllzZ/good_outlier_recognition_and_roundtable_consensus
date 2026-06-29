\# AGENTS.md



\## 适用范围

本文件仅适用于 `paper/` 目录及其子目录。



\## 当前任务定位

这里的任务主要是：

\- 中文论文生成

\- 章节重写

\- 论文结构调整

\- 结果表述与讨论表述优化

\- 按 Springer RE 期刊风格加强问题-方法-证据-贡献链条



\---



\## 写作总原则

1\. 默认输出中文

2\. 保持正式学术风格

3\. 任何写作必须服从 `project\_context.md`

4\. 任何结果表述必须以真实 outputs 为依据

5\. 不得编造引用、数字、统计量、实验观察



\---



\## 当前论文固定口径

不得擅自更改：

\- per-requirement 粒度

\- U / A / C / V 四维

\- good outlier / bad outlier framing

\- fixed weighting + dynamic-confidence roundtable

\- final problem list 作为最终输出

\- 文档级统计分析逻辑



\---



\## 各章节规则



\### Abstract

必须包含：

\- 问题

\- 方法

\- 实验设置

\- 主要发现

\- 贡献



\### Introduction

必须建立：

\- 软件工程问题背景

\- 多模型多数表决的局限

\- 当前研究缺口

\- 本文方法思路

\- 贡献点



\### Background / Related Work

必须围绕差距组织，不要只堆文献。

至少覆盖：

\- requirements quality assessment

\- LLMs in requirements engineering

\- multi-LLM / debate / consensus

\- outlier / disagreement / uncertainty handling



\### Methodology

不得偏离七阶段主线。



\### Experimental Design

必须与真实代码/输出一致。

不能写出代码里根本没有跑过的设置。



\### Results

只允许解释已有结果。

没有数据的地方必须标注待补。



\### Discussion / Validity Threats

要区分：

\- 结果解释

\- 有效性威胁

\- 局限与未来工作



\### Conclusion

只能总结已有发现。

不能引入新证据。



\---



\## 与代码和输出联动时的规则

如果论文内容涉及：

\- 实验流程

\- 参数设置

\- 输入输出格式

\- JSON 日志

\- 模型跳过 / fallback 逻辑

\- roundtable 收敛行为



则必须回看：

\- `scripts/`

\- `outputs/`

\- 对应目录下的 `AGENTS.md`



若论文描述与代码/输出不一致，先指出不一致，再建议如何改。



\---



\## 术语稳定性

优先保持这些术语不变：

\- 逐条需求（per-requirement）

\- 离群事件（outlier event）

\- 好离群（good outlier）

\- 坏离群（bad outlier）

\- 圆桌共识（roundtable consensus）

\- 动态置信度（dynamic confidence）

\- 固定全局权重（fixed global weight）

\- 团队级问题清单（team-level issue list）



\---



\## 输出时必须附带

每次完成论文任务后，必须说明：

1\. 修改了哪一部分

2\. 依据了哪些已有设定

3\. 哪些地方仍待补真实数据

4\. 哪些地方可能需要去 `scripts/` 或 `outputs/` 再核对

