# Multi-LLM Requirements Review Engineering Artifact

This repository contains an engineering research prototype for per-requirement software requirements review with multiple heterogeneous large language models. The project treats model disagreement as a structured signal and turns it into a reproducible workflow for producing a team-level issue list.

This README is focused on the engineering artifact: data flow, scripts, model orchestration, outputs, and reproducibility. It does not document manuscript drafting or writing tasks.

## Overview

Traditional multi-model aggregation often assumes that consensus is more reliable than disagreement. In requirements review, that assumption is not always sufficient: a minority model can surface a concrete, testable, and actionable issue that majority voting would suppress.

The system therefore evaluates requirements at the `per-requirement` level and uses a fixed taxonomy:

- `U`: Understandable
- `A`: Unambiguous
- `C`: Correctness
- `V`: Verifiable

The final artifact is not an averaged score. The target output is a team-level issue list containing issue type, evidence, suggested rewrite, and supporting model information.

## Core Workflow

The implemented workflow has seven stages:

1. Structured scoring: each model scores every requirement across U/A/C/V and provides confidence, rationale, evidence, and rewrite suggestions.
2. Outlier prescreening: score deviations are detected against the model-group median.
3. Argument-quality vectoring: outlier events are mapped to quality features such as evidence sufficiency, testability, rewrite actionability, specificity, taxonomy fit, and novelty.
4. Outlier classification: events are categorized as `good`, `bad`, or `uncertain` outliers.
5. Model profiling: model-level histories of good and bad outliers are converted into fixed global weights.
6. Roundtable consensus: models revise issue candidates through a roundtable process using fixed weights and dynamic confidence.
7. Final issue extraction: the system emits a consolidated issue list rather than a single document-level score.

## Engineering Components

```text
.
|-- AGENTS.md                  # project-level operating rules and invariants
|-- project_context.md          # research configuration and fixed workflow assumptions
|-- data/                       # input requirements under data/raw/
|-- outputs/                    # generated reports, logs, and round-0 caches
|-- scripts/                    # roundtable engine, scoring analysis, and utils
`-- README.md
```

Important engineering entry points:

- `scripts/roundtable/roundtable_req_reconcile.py`: main per-requirement roundtable engine (implements all treatments, the seven-stage pipeline, caching, and API calls).
- `scripts/roundtable/models.json`: model endpoint and API-key environment configuration.
- `scripts/roundtable/run_all_requirements.ps1`: batch runner for requirement CSV files (treatment selected via environment variables).
- `scripts/scoring/analyze_llm_likert_scores.py`: analyzer for initial Likert-style model scores.
- `scripts/utils/jsonl_to_xlsx_file.py`: helper to convert JSONL logs to XLSX.

## Input and Output Conventions

Requirement inputs are CSV files, typically with this schema:

```csv
item,text
R1,"Requirement text..."
R2,"Requirement text..."
```

The roundtable engine produces Excel reports and JSONL logs. Depending on the treatment, reports may include sheets such as:

- `requirements`
- `ratings_r0`
- `issues_r0`
- `final_problems`
- `outlier_events`
- `outlier_quality`
- `outlier_decisions`
- `model_profile`
- `round_history`
- `weights`

Generated outputs are written under `outputs/`. Historical outputs should be treated as versioned artifacts: any analysis should state the exact output directory or run timestamp being used.

## Experimental Treatments

The current runner supports these treatment modes:

- `single_llm`: a single configured rater is used as the baseline.
- `equal_weight_aggregation`: multiple raters are aggregated with equal weights and no roundtable.
- `roundtable_no_weighting`: roundtable consensus is used without learned/fixed model weighting.
- `full_method`: outlier-quality classification, model profiling, fixed weighting, and dynamic-confidence roundtable are enabled.

Treatment names are implemented in `scripts/roundtable/roundtable_req_reconcile.py` and used by the PowerShell batch runners.

## Environment Setup

The project currently does not define a locked dependency file. From the implemented scripts, the practical Python dependencies are:

```powershell
python -m pip install pandas numpy scipy openpyxl requests
```

Recommended runtime:

- Python 3.10 or newer
- PowerShell for batch experiment scripts
- Network access and provider API keys only when live model calls are required

Model API keys are read from environment variables configured in `scripts/roundtable/models.json`, for example:

```powershell
$env:DEEPSEEK_API_KEY="..."
$env:DASHSCOPE_API_KEY="..."
$env:OPENAI_API_KEY="..."
$env:MOONSHOT_API_KEY="..."
$env:ZHIPU_API_KEY="..."
```

For reproducible or low-cost reruns, prefer cached workflows with `outputs/caches/round0/` instead of triggering fresh model calls.

## Common Commands

> There is no test framework in this repository. Validate changes by re-running a single case and comparing the generated `outputs/` reports.

Analyze initial LLM rating files:

```powershell
python scripts/scoring/analyze_llm_likert_scores.py
```

Run the main batch workflow (default treatment `full_method`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/roundtable/run_all_requirements.ps1
```

Select a treatment / baseline via environment variables (the same runner is reused):

```powershell
$env:ROUNDTABLE_TREATMENT="single_llm"
$env:ROUNDTABLE_SINGLE_RATER="qwen"
powershell -ExecutionPolicy Bypass -File scripts/roundtable/run_all_requirements.ps1
# valid treatments: single_llm, equal_weight_aggregation, roundtable_no_weighting, full_method
```

Force the runner to use existing round-0 cache entries (no live API calls):

```powershell
$env:ROUNDTABLE_REQUIRE_ROUND0_CACHE="true"
powershell -ExecutionPolicy Bypass -File scripts/roundtable/run_all_requirements.ps1
```

Run a single requirement file directly (bypassing the batch runner, useful for debugging):

```powershell
python scripts/roundtable/roundtable_req_reconcile.py --requirements <case.csv> `
  --models scripts/roundtable/models.json --out report.xlsx --out_dir logs `
  --treatment full_method --round0_cache_dir outputs/caches/round0 `
  --topk 10 --rounds 2 --theta_ratio 0.6 --eps_score 0.25 --tau_jacc 0.9
```

## Reproducibility Rules

- Do not overwrite `data/raw/` files.
- If any ground-truth assets are added, treat them as high-sensitivity data; do not regenerate them without checking the intended version.
- Prefer adding a new timestamped output directory over overwriting historical outputs.
- When reporting metrics, always reference the exact `outputs/` directory and generated report.
- Do not infer unsupported conclusions from partial outputs or incomplete treatment coverage.
- Keep `per-requirement` granularity and the U/A/C/V taxonomy fixed unless the research configuration is intentionally changed.

## Current Status

The repository is an active engineering research prototype. It includes executable scripts, cached model outputs, and treatment comparison artifacts under `outputs/`. There is currently no automated test suite. Some result directories are historical runs and should be interpreted only with their path and timestamp.

## License

No open-source license is currently declared in this artifact. Add a license and data-use statement before public release.
