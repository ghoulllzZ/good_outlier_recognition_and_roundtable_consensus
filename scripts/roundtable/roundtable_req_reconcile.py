# roundtable_req_reconcile.py
# -*- coding: utf-8 -*-
"""
Per-requirement roundtable (RECONCILE-style) for requirement quality auditing.

Pipeline:
A) Initial scoring per requirement by multiple LLMs:
   - scores (U/A/C/V), confidences (U/A/C/V), rationales, rewrites, issues (taxonomy=U/A/C/V)
B) Fixed rater weights:
   - Option 1: read weights.csv
   - Option 2: compute from llm_rating_report.xlsx (analyze_llm_likert_scores output) + human labels:
       model_labels.csv (model-level) + hotcell_labels.csv (hot cells only)
C) Roundtable:
   - Discuss TOP-K issue candidates (item, type) ranked by influence = w_r * recalibrated_conf
   - In each round, each rater updates scores/confidences/issues for provided items
   - Stop if any: score convergence OR issue convergence OR max rounds
D) Final output:
   - item + issue_type(U/A/C/V) + evidence + rewrite (issue aggregated score >= threshold)
   - Excel report

OpenAI-compatible endpoint: POST {base_url}/v1/chat/completions
"""

import os
import re
import json
import ast
import math
import random
import time
import argparse
import requests
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Dict, List, Any, Optional, Tuple

import numpy as np
import pandas as pd
import requests


# =========================
# 0) Constants / Taxonomy
# =========================
DIMENSIONS = ["Understandable", "Unambiguous", "Correctness", "Verifiable"]
DIM_SHORT = {
    "Understandable": "U",
    "Unambiguous": "A",
    "Correctness": "C",
    "Verifiable": "V",
}
SHORT_DIM = {v: k for k, v in DIM_SHORT.items()}
INPUT_DIM_ALIASES = {
    **DIM_SHORT,
    "可理解性": "U",
    "无歧义": "A",
    "正确性": "C",
    "可验证性": "V",
}

DEFAULT_TOPK_ISSUES = 10
DEFAULT_MAX_ROUNDS = 2

# Issue aggregation threshold: Theta = 0.6 * sum(weights); if weights normalized sum=1 -> 0.6
DEFAULT_THETA_RATIO = 0.6

# Convergence
DEFAULT_EPS_SCORE = 0.25  # score convergence epsilon
DEFAULT_TAU_JACCARD = 0.9  # issue-set convergence

# Outlier & argument-quality workflow (fixed by design)
DEFAULT_OUTLIER_DELTA = 2
DEFAULT_TAU_GOOD = 0.60
DEFAULT_TAU_BAD = 0.30
DEFAULT_LAMBDA_BAD = 1.5
DEFAULT_BETA_WEIGHT = 1.0
DEFAULT_WEIGHT_SCORE_PRIOR = 3.0
DEFAULT_DOC_EVIDENCE_PRIOR = 4.0


@dataclass(frozen=True)
class TreatmentConfig:
    key: str
    label: str
    use_roundtable: bool
    use_outlier_pipeline: bool
    use_quality_scores: bool
    recompute_weights: bool
    fixed_candidate_pairs: bool
    force_equal_weights: bool
    requires_single_rater: bool = False


TREATMENT_CONFIGS: Dict[str, TreatmentConfig] = {
    "single_llm": TreatmentConfig(
        key="single_llm",
        label="Single-LLM",
        use_roundtable=False,
        use_outlier_pipeline=False,
        use_quality_scores=False,
        recompute_weights=False,
        fixed_candidate_pairs=False,
        force_equal_weights=True,
        requires_single_rater=True,
    ),
    "equal_weight_aggregation": TreatmentConfig(
        key="equal_weight_aggregation",
        label="Equal-Weight Aggregation",
        use_roundtable=False,
        use_outlier_pipeline=False,
        use_quality_scores=False,
        recompute_weights=False,
        fixed_candidate_pairs=False,
        force_equal_weights=True,
    ),
    "roundtable_no_weighting": TreatmentConfig(
        key="roundtable_no_weighting",
        label="Roundtable w/o Weighting",
        use_roundtable=True,
        use_outlier_pipeline=False,
        use_quality_scores=False,
        recompute_weights=False,
        fixed_candidate_pairs=False,
        force_equal_weights=True,
    ),
    "full_method": TreatmentConfig(
        key="full_method",
        label="Full Method",
        use_roundtable=True,
        use_outlier_pipeline=True,
        use_quality_scores=True,
        recompute_weights=True,
        fixed_candidate_pairs=True,
        force_equal_weights=False,
    ),
}

TREATMENT_ALIASES = {
    "single_llm": "single_llm",
    "single_llm_baseline": "single_llm",
    "single-llm": "single_llm",
    "singlellm": "single_llm",
    "equal_weight_aggregation": "equal_weight_aggregation",
    "equal-weight-aggregation": "equal_weight_aggregation",
    "equal_weight": "equal_weight_aggregation",
    "equal-weight": "equal_weight_aggregation",
    "roundtable_no_weighting": "roundtable_no_weighting",
    "roundtable-no-weighting": "roundtable_no_weighting",
    "roundtable_wo_weighting": "roundtable_no_weighting",
    "roundtable_w_o_weighting": "roundtable_no_weighting",
    "roundtable_without_weighting": "roundtable_no_weighting",
    "full_method": "full_method",
    "full-method": "full_method",
    "full": "full_method",
}

OUTLIER_EVENT_COLUMNS = [
    "rater", "item", "dim", "dimension", "score", "confidence",
    "baseline_median", "deviation", "abs_deviation", "delta",
    "rationale", "suggestion",
]
OUTLIER_QUALITY_COLUMNS = [
    "rater", "item", "dim", "abs_deviation", "E_evidence", "F_falsifiable",
    "R_rewrite_exec", "S_specificity", "T_taxonomy_fit", "D_novelty", "Q", "hard_fail",
]
OUTLIER_DECISION_COLUMNS = ["rater", "item", "dim", "abs_deviation", "Q", "hard_fail", "decision"]
MODEL_PROFILE_COLUMNS = ["rater", "G", "B", "weight_raw", "weight", "n_good", "n_bad", "n_uncertain", "n_outliers"]
Q_MAP_COLUMNS = ["rater", "item", "type", "Q"]
CANDIDATE_PAIR_COLUMNS = ["item", "type"]


def canonicalize_treatment_name(value: str) -> str:
    token = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    mapped = TREATMENT_ALIASES.get(token)
    if not mapped:
        valid = ", ".join(sorted(TREATMENT_CONFIGS))
        raise ValueError(f"Unknown treatment '{value}'. Valid values: {valid}")
    return mapped


def resolve_treatment_config(value: str) -> TreatmentConfig:
    return TREATMENT_CONFIGS[canonicalize_treatment_name(value)]



# Confidence recalibration (RECONCILE-style)
def recalibrate_conf(p: float) -> float:
    p = float(p)
    if p >= 1.0:
        return 1.0
    if p >= 0.9:
        return 0.8
    if p >= 0.8:
        return 0.5
    if p > 0.6:
        return 0.3
    return 0.1


def normalize_weight_map(raters: List[str], weights: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    if not raters:
        return {}
    if not weights:
        return {r: 1.0 / len(raters) for r in raters}

    vals = []
    for r in raters:
        try:
            vals.append(max(float(weights.get(r, 0.0)), 0.0))
        except (TypeError, ValueError):
            vals.append(0.0)

    total = float(sum(vals))
    if total <= 0.0:
        return {r: 1.0 / len(raters) for r in raters}
    return {r: v / total for r, v in zip(raters, vals)}


def empty_df(columns: List[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def select_treatment_raters(
    raters: List["Rater"],
    treatment: TreatmentConfig,
    single_rater: str = "",
) -> List["Rater"]:
    if not treatment.requires_single_rater:
        if single_rater:
            raise ValueError("--single_rater is only valid with --treatment single_llm")
        return raters

    if single_rater:
        wanted = single_rater.strip().lower()
        chosen = [r for r in raters if r.name.strip().lower() == wanted]
        if not chosen:
            names = ", ".join(r.name for r in raters)
            raise ValueError(f"Unknown single rater '{single_rater}'. Available raters: {names}")
        return chosen

    if len(raters) == 1:
        return raters

    names = ", ".join(r.name for r in raters)
    raise ValueError(f"--treatment single_llm requires --single_rater when multiple raters are configured: {names}")


def stable_softmax(scores: np.ndarray) -> np.ndarray:
    if scores.size == 0:
        return scores
    arr = np.asarray(scores, dtype=float)
    arr = arr - np.nanmax(arr)
    raw = np.exp(arr)
    total = float(np.nansum(raw))
    if total <= 0.0 or not np.isfinite(total):
        return np.full(arr.shape, 1.0 / len(arr), dtype=float)
    return raw / total


# Hot-cell criteria (agree with your default)
HOTCELL_RULES = {
    "range_ge": 2.0,
    "iqr_ge": 1.0,
    "abs_delta_ge": 2.0,
}

# Weight computation hyperparams (only used if not providing weights.csv)
LAMBDA_BAD_GLOBAL = 1.2
LAMBDA_BAD_HOT = 1.0
LAMBDA_GOOD_HOT = 0.6

# For OutlierIndex built from analyze report metrics (min-max normalized, then weighted sum)
OUTLIER_METRICS_WEIGHTS = {
    "avg_distance_to_others": 0.35,
    "contribution(G-G_minus)": 0.35,
    "bias_abs": 0.15,
    "mean_abs_delta": 0.15,
}

# Network / retry
TEMPERATURE_SCORE = 0.2
TEMPERATURE_DISCUSS = 0.2
DEFAULT_REQUEST_TIMEOUT = 120.0
MAX_RETRIES = 2
RETRY_SLEEP = 2.0
DEFAULT_MIN_REQUEST_INTERVAL = 1.5
MAX_429_RETRIES = 4
RETRY_429_BASE_SLEEP = 5.0
RETRY_429_MAX_SLEEP = 60.0
RETRY_429_JITTER = 0.5
MAX_TIMEOUT_RETRIES = 2
RETRY_TIMEOUT_BASE_SLEEP = 10.0
RETRY_TIMEOUT_MAX_SLEEP = 90.0
RETRY_TIMEOUT_JITTER = 1.0


# =========================
# 1) Providers
# =========================
class LLMProvider:
    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        raise NotImplementedError


class OpenAICompatProvider(LLMProvider):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        min_interval_sec: float = DEFAULT_MIN_REQUEST_INTERVAL,
        max_429_retries: int = MAX_429_RETRIES,
        retry_429_base_sleep: float = RETRY_429_BASE_SLEEP,
        retry_429_max_sleep: float = RETRY_429_MAX_SLEEP,
        max_timeout_retries: int = MAX_TIMEOUT_RETRIES,
        retry_timeout_base_sleep: float = RETRY_TIMEOUT_BASE_SLEEP,
        retry_timeout_max_sleep: float = RETRY_TIMEOUT_MAX_SLEEP,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = max(float(timeout), 1.0)
        self.min_interval_sec = max(float(min_interval_sec), 0.0)
        self.max_429_retries = max(int(max_429_retries), 0)
        self.retry_429_base_sleep = max(float(retry_429_base_sleep), 0.0)
        self.retry_429_max_sleep = max(float(retry_429_max_sleep), self.retry_429_base_sleep)
        self.max_timeout_retries = max(int(max_timeout_retries), 0)
        self.retry_timeout_base_sleep = max(float(retry_timeout_base_sleep), 0.0)
        self.retry_timeout_max_sleep = max(float(retry_timeout_max_sleep), self.retry_timeout_base_sleep)
        self._next_request_ts = 0.0

    def _throttle(self) -> None:
        if self.min_interval_sec <= 0:
            return
        now = time.monotonic()
        if self._next_request_ts > now:
            time.sleep(self._next_request_ts - now)
        self._next_request_ts = time.monotonic() + self.min_interval_sec

    def chat(self, messages: List[Dict[str, str]], temperature: float = 0.2) -> str:
        # url = f"{self.base_url}/chat/completions"
        self._throttle()

        base = self.base_url
        # tolerate base_url with or without "/v1"
        if base.endswith("/v1"):
            url = f"{base}/chat/completions"
        else:
            url = f"{base}/chat/completions"

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "messages": messages, "temperature": float(temperature)}
        resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


@dataclass
class Rater:
    name: str
    provider: Optional[LLMProvider]


# =========================
# 2) Utilities / IO
# =========================
def normalize_item(x: str) -> str:
    x = str(x).strip()
    x = x.replace("：", ":")
    m = re.match(r"^(R|C)\s*0*(\d+)\b", x, flags=re.I)
    if m:
        return m.group(1).upper() + str(int(m.group(2)))
    return x


def read_csv_smart(path: str) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    return pd.read_csv(path)


def safe_path_component(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text or "NA"


def round0_cache_payload_path(cache_dir: str, rater_name: str, item: str) -> str:
    rater_dir = os.path.join(cache_dir, safe_path_component(rater_name))
    return os.path.join(rater_dir, f"{safe_path_component(item)}.json")


def round0_cache_error_path(cache_dir: str, rater_name: str, item: str) -> str:
    rater_dir = os.path.join(cache_dir, safe_path_component(rater_name))
    return os.path.join(rater_dir, f"{safe_path_component(item)}.error.json")


def load_round0_cache_entry(cache_dir: str, rater_name: str, item: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    payload_path = round0_cache_payload_path(cache_dir, rater_name, item)
    error_path = round0_cache_error_path(cache_dir, rater_name, item)

    if os.path.exists(payload_path):
        with open(payload_path, "r", encoding="utf-8") as f:
            cached = json.load(f)
        payload = cached.get("payload", cached) if isinstance(cached, dict) else cached
        payload = validate_scoring_payload(payload, rater_name)
        return "hit", payload

    if os.path.exists(error_path):
        with open(error_path, "r", encoding="utf-8") as f:
            cached_err = json.load(f)
        return "skip", cached_err if isinstance(cached_err, dict) else {"error": str(cached_err)}

    return "miss", None


def save_round0_cache_payload(cache_dir: str, rater_name: str, item: str, payload: Dict[str, Any]) -> str:
    path = round0_cache_payload_path(cache_dir, rater_name, item)
    ensure_dir(os.path.dirname(path))
    record = {
        "schema_version": 1,
        "rater": rater_name,
        "item": item,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "payload": payload,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    err_path = round0_cache_error_path(cache_dir, rater_name, item)
    if os.path.exists(err_path):
        os.remove(err_path)
    return path


def save_round0_cache_error(cache_dir: str, rater_name: str, item: str, error_text: str) -> str:
    path = round0_cache_error_path(cache_dir, rater_name, item)
    ensure_dir(os.path.dirname(path))
    record = {
        "schema_version": 1,
        "rater": rater_name,
        "item": item,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "error": str(error_text),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    payload_path = round0_cache_payload_path(cache_dir, rater_name, item)
    if os.path.exists(payload_path):
        os.remove(payload_path)
    return path


def get_http_status(exc: Exception) -> Optional[int]:
    if not isinstance(exc, requests.exceptions.HTTPError):
        return None
    return getattr(getattr(exc, "response", None), "status_code", None)


def parse_retry_after_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(float(text), 0.0)
    except (TypeError, ValueError):
        pass
    try:
        retry_dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_dt is None:
        return None
    if retry_dt.tzinfo is None:
        retry_dt = retry_dt.replace(tzinfo=timezone.utc)
    delta = (retry_dt - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


def compute_retry_delay_seconds(
    exc: Exception,
    attempt_idx: int,
    default_sleep: float = RETRY_SLEEP,
    retry_429_base_sleep: float = RETRY_429_BASE_SLEEP,
    retry_429_max_sleep: float = RETRY_429_MAX_SLEEP,
    retry_timeout_base_sleep: float = RETRY_TIMEOUT_BASE_SLEEP,
    retry_timeout_max_sleep: float = RETRY_TIMEOUT_MAX_SLEEP,
) -> float:
    status = get_http_status(exc)
    if isinstance(exc, requests.exceptions.ReadTimeout):
        backoff = retry_timeout_base_sleep * (2 ** max(int(attempt_idx), 0))
        jitter = random.uniform(0.0, RETRY_TIMEOUT_JITTER)
        return min(retry_timeout_max_sleep, backoff + jitter)
    if status == 429:
        retry_after = parse_retry_after_seconds(
            getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After")
        )
        if retry_after is not None and retry_after > 0:
            return retry_after
        backoff = retry_429_base_sleep * (2 ** max(int(attempt_idx), 0))
        jitter = random.uniform(0.0, RETRY_429_JITTER)
        return min(retry_429_max_sleep, backoff + jitter)
    return max(float(default_sleep), 0.0)


def load_requirements_csv(path: str) -> pd.DataFrame:
    df = read_csv_smart(path)
    need = ["item", "text"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise ValueError(f"{path} missing columns {miss}; got columns={list(df.columns)}")
    df = df.copy()
    df["item"] = df["item"].apply(normalize_item)
    df["text"] = df["text"].astype(str)
    return df[["item", "text"]]


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)

def _strip_code_fence(s: str) -> str:
    s = (s or "").strip()
    m = _JSON_FENCE_RE.search(s)
    if m:
        return m.group(1).strip()
    return s

def _extract_balanced_json(s: str) -> str:
    """
    Extract the first balanced JSON-like span starting from the first '{' or '['.
    If unclosed (model output truncated), return span to end and auto-close braces/brackets.
    This function ignores braces/brackets inside string literals.
    """
    i0 = None
    for i, ch in enumerate(s):
        if ch in "{[":
            i0 = i
            break
    if i0 is None:
        raise ValueError("No JSON start '{' or '[' found.")

    stack: List[str] = []
    in_str = False
    esc = False

    pairs = {"{": "}", "[": "]"}
    closers = set(pairs.values())

    for j in range(i0, len(s)):
        ch = s[j]

        if in_str:
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = False
            continue

        # not in string
        if ch == '"':
            in_str = True
            continue

        if ch in pairs:
            stack.append(ch)
            continue

        if ch in closers:
            if stack and pairs.get(stack[-1]) == ch:
                stack.pop()
                if not stack:
                    return s[i0 : j + 1]
            else:
                # mismatched closer, ignore
                continue

    # If we reached end and still unclosed, we assume truncation; auto-close
    span = s[i0:]
    if stack:
        span += "".join(pairs[o] for o in reversed(stack))
    return span

def _normalize_fullwidth_punct_outside_ascii_strings(s: str) -> str:
    """
    只在 ASCII 双引号字符串之外替换全角标点，避免破坏字符串内容。
    """
    mapping = {
        "：": ":",
        "，": ",",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
    out = []
    in_str = False
    esc = False

    for ch in s:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
                out.append(ch)
            else:
                out.append(mapping.get(ch, ch))

    return "".join(out)


def _escape_unescaped_quotes_in_ascii_strings(s: str) -> str:
    """
    修复“字符串值内部出现未转义的 ASCII 双引号”的情况。
    规则：在字符串内遇到 `"` 时，如果它后面不是 , } ]（可跳过空白），则视为内容引号，转义成 \\"
    """
    out = []
    in_str = False
    esc = False
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]

        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
                esc = False
            i += 1
            continue

        # in string
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue

        if ch == "\\":
            out.append(ch)
            esc = True
            i += 1
            continue

        if ch == '"':
            # look ahead to decide closing vs inner quote
            j = i + 1
            while j < n and s[j] in " \t\r\n":
                j += 1
            if j < n and s[j] in ",}]":
                # closing quote
                out.append(ch)
                in_str = False
            else:
                # inner quote -> escape it
                out.append('\\"')
            i += 1
            continue

        out.append(ch)
        i += 1

    return "".join(out)



def extract_first_json(text: str) -> Dict[str, Any]:
    s = _strip_code_fence(text)

    # normalize common full-width quotes
    # DO NOT convert Chinese “ ” to ASCII quotes: it breaks valid JSON strings.
    s = s.replace("’", "'").replace("‘", "'")

    def _normalize_punct_outside_strings(x: str) -> str:
        # Convert full-width colon/comma outside strings to ASCII (helps Chinese outputs)
        out = []
        in_str = False
        esc = False
        for ch in x:
            if in_str:
                out.append(ch)
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            else:
                if ch == '"':
                    in_str = True
                    out.append(ch)
                    continue
                if ch == "：":
                    out.append(":")
                elif ch == "，":
                    out.append(",")
                elif ch == "｛":
                    out.append("{")
                elif ch == "｝":
                    out.append("}")
                elif ch == "［":
                    out.append("[")
                elif ch == "］":
                    out.append("]")
                else:
                    out.append(ch)
        return "".join(out)

    # Try multiple candidate starts to avoid picking '{' from explanation text.
    # But never fall back to nested objects inside an earlier outer candidate:
    # if the outer payload is malformed, we want repair/retry, not "scores" only.
    starts = [m.start() for m in re.finditer(r"[\{\[]", s)]
    if not starts:
        raise ValueError("No JSON start found in model output.")

    last_err = None
    tried_spans: List[Tuple[int, int]] = []
    for idx in starts[:50]:  # cap to avoid pathological long texts
        if any(lo <= idx < hi for lo, hi in tried_spans):
            continue
        try:
            span = _extract_balanced_json(s[idx:])
            tried_spans.append((idx, idx + len(span)))
            span = _normalize_punct_outside_strings(span)

            # remove trailing commas before } or ]
            span = re.sub(r",\s*([}\]])", r"\1", span)

            # 1) strict JSON
            try:
                obj = json.loads(span)
            except json.JSONDecodeError:
                # 2) relaxed python-literal fallback
                s2 = span
                s2 = re.sub(r"\bnull\b", "None", s2, flags=re.I)
                s2 = re.sub(r"\btrue\b", "True", s2, flags=re.I)
                s2 = re.sub(r"\bfalse\b", "False", s2, flags=re.I)
                obj = ast.literal_eval(s2)

            if isinstance(obj, dict):
                return obj
            # If top-level is a list (some models output items array directly), wrap it.
            if isinstance(obj, list):
                return {"items": obj}
            raise ValueError(f"Top-level parsed value is not an object: {type(obj)}")

        except Exception as e:
            last_err = e
            continue

    raise ValueError(f"Failed to parse JSON from model output. Last error: {last_err}")





def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)


# =========================
# 3) Prompting: scoring + discussion
# =========================
SCORING_SYSTEM = "你是严谨的需求质量评审专家（软件需求工程方向）。必须引用需求原文片段作为证据，改写必须可执行、原子、可测试。只输出JSON。"

SCORING_USER_TEMPLATE = """你将对下面这条需求进行质量评审（Likert 1-5，5为最好），并输出需要修改的问题清单（issues）。
taxonomy固定为四类：Understandable(U)、Unambiguous(A)、Correctness(C)、Verifiable(V)。

要求：
1) 每个维度给出 score(1-5) 与 confidence(0-1)；
    - 注意：必须输出“具体数值”，严禁写成 1-5 / 0-1 这种范围表达（那不是合法JSON）；
2) rationale 必须包含“证据片段”（引用原文中的关键短语/句子）并解释为何影响该维度；
3) suggestion 必须给出“可直接替换的改写文本”（更原子、可测试、无歧义），若你认为无需修改该维度，可给出 minimal suggestion（例如“保持不变”）；
4) issues：只列出你认为“需要改”的维度（taxonomy=U/A/C/V），每条包含 type、evidence、rewrite。
5) 只输出JSON，不要输出其他文本。
6)在任何字符串值中不得出现未转义的 ASCII 双引号 "；如需引用请使用中文引号“”或单引号''，或写成 \\\"。

需求条目：{item}
需求文本：{text}

输出JSON schema：
{{
  "item": "{item}",
  "scores": {{"U": 3, "A": 3, "C": 3, "V": 3}},
  "confidences": {{"U": 0.7, "A": 0.7, "C": 0.7, "V": 0.7}},
  "rationales": {{"U":"...","A":"...","C":"...","V":"..."}},
  "suggestions": {{"U":"...","A":"...","C":"...","V":"..."}},
  "issues": [
    {{"type":"U|A|C|V","evidence":"引用原文片段","rewrite":"改写文本"}}
  ]
}}
"""

DISCUSS_SYSTEM = "你是圆桌会议中的需求质量评审专家。你将看到其他模型对若干“需要更改的需求问题(issues)”的观点。请基于证据更新你的评分、置信度和改写建议。只输出JSON。"

DISCUSS_USER_TEMPLATE = """圆桌会议第 {round_idx} 轮。下面给出 Top-{topk} 个“需要更改的 issues”（按 influence=weight×recalibrated_conf 排序）。
注意：这里不强调维度分组，你需要对每个 issue 所属条目进行整体更新（但仍需输出U/A/C/V四维度的scores/confidences与issues，其中 U=Understandable，A=Unambiguous）。

Top issues:
{issues_block}

你的任务：
1) 对每个条目，结合他人证据，更新你对该条目的 scores/confidences/rationales/suggestions；
2) 更新 issues 列表：只保留你认为“仍需要改”的维度，证据必须引用原文或他人给出的引用片段；
3) 如果你改变了观点，请说明是什么证据导致你改变，并相应调整confidence；
4) 只输出JSON，格式如下（items数组顺序任意，但必须覆盖给出的条目）：

{{
  "round": {round_idx},
  "items": [
    {{
      "item":"R1",
      "scores": {{"U": 3, "A": 3, "C": 3, "V": 3}},
      "confidences": {{"U": 0.7, "A": 0.7, "C": 0.7, "V": 0.7}},
      "rationales": {{"U":"...","A":"...","C":"...","V":"..."}},
      "suggestions": {{"U":"...","A":"...","C":"...","V":"..."}},
      "issues":[{{"type":"U|A|C|V","evidence":"...","rewrite":"..."}}]
    }}
  ]
}}
"""



def _append_jsonl(path: str, rec: Dict[str, Any]):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _repair_to_strict_json(rater: Rater, bad_text: str) -> str:
    fix_messages = [
        {"role": "system", "content": "你是严格JSON修复器。你只负责把输入文本转换为严格可解析的JSON。禁止输出除JSON之外的任何字符。"},
        {"role": "user", "content": "把下面文本修复为严格JSON（双引号、无尾逗号、无注释、无代码块）。只输出JSON：\n\n" + (bad_text or "")}
    ]
    return rater.provider.chat(fix_messages, temperature=0.0)

def _append_jsonl(path: str, rec: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def dump_invalid_output(
    out_dir: Optional[str],
    stage: str,
    rater_name: str,
    item: Optional[str],
    payload: Any,
):
    if not out_dir:
        return
    os.makedirs(out_dir, exist_ok=True)
    ts = int(time.time() * 1000)
    path = os.path.join(out_dir, f"invalid_output_{stage}_{rater_name}_{item or 'NA'}_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def is_skippable_rater_error(exc: Exception) -> bool:
    return "(skip)" in str(exc)

def validate_scoring_payload(data: Any, rater_name: str) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise RuntimeError(f"[{rater_name}] INVALID OUTPUT (skip): payload is not a JSON object")
    if "scores" not in data or "confidences" not in data:
        raise RuntimeError(f"[{rater_name}] INVALID OUTPUT (skip): missing scores/confidences")
    if not isinstance(data["scores"], dict) or not isinstance(data["confidences"], dict):
        raise RuntimeError(f"[{rater_name}] INVALID OUTPUT (skip): scores/confidences must be objects")
    for sdim in ["U", "A", "C", "V"]:
        if sdim not in data["scores"] or sdim not in data["confidences"]:
            raise RuntimeError(f"[{rater_name}] INVALID OUTPUT (skip): missing {sdim}")
    return data

def call_llm_json(
    rater: Rater,
    messages: List[Dict[str, str]],
    temperature: float,
    out_dir: Optional[str] = None,
    stage: str = "unknown",
    round_idx: Optional[int] = None,
    item: Optional[str] = None,
) -> Dict[str, Any]:
    last_err = None
    log_path = os.path.join(out_dir, "raw_logs.jsonl") if out_dir else None
    generic_retries = 0
    rate_limit_retries = 0
    timeout_retries = 0
    attempt_idx = 0

    while True:
        txt = None
        fixed = None
        try:
            txt = rater.provider.chat(messages, temperature=temperature)

            # 1) 总是记录 raw 输出（这样“成功也有日志”）
            if log_path:
                _append_jsonl(log_path, {
                    "stage": stage,
                    "round": round_idx,
                    "item": item,
                    "rater": rater.name,
                    "attempt": attempt_idx,
                    "temperature": float(temperature),
                    "kind": "raw",
                    "text": txt[:20000],
                })

            try:
                data = extract_first_json(txt)
                if log_path:
                    _append_jsonl(log_path, {
                        "stage": stage, "round": round_idx, "item": item,
                        "rater": rater.name, "attempt": attempt_idx,
                        "kind": "parsed_ok"
                    })
                return data

            except Exception as e1:
                # 2) 解析失败 → 修复一次再解析
                fixed = _repair_to_strict_json(rater, txt)

                if log_path:
                    _append_jsonl(log_path, {
                        "stage": stage,
                        "round": round_idx,
                        "item": item,
                        "rater": rater.name,
                        "attempt": attempt_idx,
                        "kind": "fixed",
                        "text": fixed[:20000],
                        "error_raw_parse": str(e1),
                    })

                data = extract_first_json(fixed)
                if log_path:
                    _append_jsonl(log_path, {
                        "stage": stage, "round": round_idx, "item": item,
                        "rater": rater.name, "attempt": attempt_idx,
                        "kind": "fixed_parsed_ok"
                    })
                return data

        except Exception as e:
            last_err = e

            # 3) 失败也记一条
            if log_path:
                _append_jsonl(log_path, {
                    "stage": stage,
                    "round": round_idx,
                    "item": item,
                    "rater": rater.name,
                    "attempt": attempt_idx,
                    "kind": "error",
                    "error": str(e),
                })

            # 4) 同时把完整文本落盘（便于手工排查）
            if out_dir:
                os.makedirs(out_dir, exist_ok=True)
                ts = int(time.time() * 1000)

                if txt:
                    p1 = os.path.join(out_dir, f"bad_json_{stage}_{rater.name}_{item or 'NA'}_raw_a{attempt_idx}_{ts}.txt")
                    with open(p1, "w", encoding="utf-8") as f:
                        f.write(txt)

                if fixed:
                    p2 = os.path.join(out_dir, f"bad_json_{stage}_{rater.name}_{item or 'NA'}_fixed_a{attempt_idx}_{ts}.txt")
                    with open(p2, "w", encoding="utf-8") as f:
                        f.write(fixed)

            status_code = get_http_status(e)
            if isinstance(e, requests.exceptions.ReadTimeout):
                max_timeout_retries = getattr(rater.provider, "max_timeout_retries", MAX_TIMEOUT_RETRIES)
                if timeout_retries >= max_timeout_retries:
                    break
                timeout_retries += 1
                sleep_s = compute_retry_delay_seconds(
                    e,
                    attempt_idx=attempt_idx,
                    default_sleep=RETRY_SLEEP,
                    retry_timeout_base_sleep=getattr(rater.provider, "retry_timeout_base_sleep", RETRY_TIMEOUT_BASE_SLEEP),
                    retry_timeout_max_sleep=getattr(rater.provider, "retry_timeout_max_sleep", RETRY_TIMEOUT_MAX_SLEEP),
                )
            elif status_code == 429:
                max_429_retries = getattr(rater.provider, "max_429_retries", MAX_429_RETRIES)
                if rate_limit_retries >= max_429_retries:
                    break
                rate_limit_retries += 1
                sleep_s = compute_retry_delay_seconds(
                    e,
                    attempt_idx=attempt_idx,
                    default_sleep=RETRY_SLEEP,
                    retry_429_base_sleep=getattr(rater.provider, "retry_429_base_sleep", RETRY_429_BASE_SLEEP),
                    retry_429_max_sleep=getattr(rater.provider, "retry_429_max_sleep", RETRY_429_MAX_SLEEP),
                )
            else:
                if generic_retries >= MAX_RETRIES:
                    break
                generic_retries += 1
                sleep_s = compute_retry_delay_seconds(
                    e,
                    attempt_idx=attempt_idx,
                    default_sleep=RETRY_SLEEP,
                )

            time.sleep(sleep_s)
            attempt_idx += 1

    if isinstance(last_err, requests.exceptions.ReadTimeout):
        raise RuntimeError(f"[{rater.name}] TIMEOUT (skip): {last_err}") from last_err
    if isinstance(last_err, requests.exceptions.HTTPError):
        status = get_http_status(last_err)
        status_text = f"HTTP {status}" if status is not None else "HTTP ERROR"
        raise RuntimeError(f"[{rater.name}] {status_text} (skip): {last_err}") from last_err
    if isinstance(last_err, requests.exceptions.RequestException):
        raise RuntimeError(f"[{rater.name}] REQUEST ERROR (skip): {last_err}") from last_err

    raise RuntimeError(f"[{rater.name}] failed after retries: {last_err}")





def score_requirement(rater: Rater, item: str, text: str, out_dir: Optional[str] = None) -> Dict[str, Any]:
    if rater.provider is None:
        raise RuntimeError(f"[{rater.name}] Missing provider/API key for live scoring")
    messages = [
        {"role": "system", "content": SCORING_SYSTEM},
        {"role": "user", "content": SCORING_USER_TEMPLATE.format(item=item, text=text)},
    ]
    # IMPORTANT: pass out_dir so raw/fixed/bad_json logs are persisted
    data = call_llm_json(rater, messages, temperature=TEMPERATURE_SCORE, out_dir=out_dir, stage="score", item=item)

    # --- [MIN PATCH] unwrap payload / alias keys / dim keys ---
    # 0) unwrap common wrapper keys
    if isinstance(data, dict):
        for k in ("result", "data", "output", "payload"):
            if isinstance(data.get(k), dict):
                data = data[k]
                break

    # 1) discuss-style {"items":[...]} OR top-level list -> pick the matching item
    items_list = None
    if isinstance(data, list):
        items_list = data
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        items_list = data["items"]

    if isinstance(items_list, list):
        want = normalize_item(item)
        target = None
        for it in items_list:
            if isinstance(it, dict) and normalize_item(it.get("item", "")) == want:
                target = it
                break
        if target is None and len(items_list) == 1 and isinstance(items_list[0], dict):
            target = items_list[0]
        if target is not None:
            data = target

    # 2) Alias compatibility: score/confidence -> scores/confidences
    if isinstance(data, dict):
        if "scores" not in data and "score" in data:
            data["scores"] = data["score"]
        if "confidences" not in data and "confidence" in data:
            data["confidences"] = data["confidence"]

        # 3) (optional but recommended) remap dimension full names -> U/A/C/V
        def _remap_dims(d):
            if not isinstance(d, dict):
                return d
            return {INPUT_DIM_ALIASES.get(k, k): v for k, v in d.items()}

        if "scores" in data:
            data["scores"] = _remap_dims(data["scores"])
        if "confidences" in data:
            data["confidences"] = _remap_dims(data["confidences"])
    # --- [MIN PATCH END] ---

    try:
        return validate_scoring_payload(data, rater.name)
    except RuntimeError:
        dump_invalid_output(out_dir, "score", rater.name, item, data)
        raise


# =========================
# 4) Build state tables
# =========================
def flatten_scoring_output(rater: str, item: str, data: Dict[str, Any]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns:
      - rating_long: rows per dim (score/confidence/rationale/suggestion)
      - issues_long: rows per issue (type/evidence/rewrite)
    """
    rows = []
    for sdim in ["U", "A", "C", "V"]:
        rows.append({
            "rater": rater,
            "item": item,
            "dim": sdim,
            "dimension": SHORT_DIM[sdim],
            "score": float(data["scores"][sdim]),
            "confidence": float(data["confidences"][sdim]),
            "rationale": str(data.get("rationales", {}).get(sdim, ""))[:4000],
            "suggestion": str(data.get("suggestions", {}).get(sdim, ""))[:4000],
        })
    rating_long = pd.DataFrame(rows)

    issues = data.get("issues", []) or []
    irows = []
    for it in issues:
        t = str(it.get("type", "")).strip().upper()
        if t not in ["U", "A", "C", "V"]:
            continue
        irows.append({
            "rater": rater,
            "item": item,
            "type": t,
            "dimension": SHORT_DIM[t],
            "evidence": str(it.get("evidence", ""))[:2000],
            "rewrite": str(it.get("rewrite", ""))[:4000],
        })
    issues_long = pd.DataFrame(irows) if irows else pd.DataFrame(columns=["rater","item","type","dimension","evidence","rewrite"])
    return rating_long, issues_long


def weighted_team_score(ratings_long: pd.DataFrame, weights: Dict[str, float]) -> pd.DataFrame:
    """
    Team score per item×dim using w * recalibrated_conf.
    """
    df = ratings_long.copy()
    df["w"] = df["rater"].map(weights).fillna(0.0)
    df["pc"] = df["confidence"].apply(recalibrate_conf)
    df["cw"] = df["w"] * df["pc"]

    def _agg(g: pd.DataFrame) -> pd.Series:
        denom = float(g["cw"].sum())
        if denom <= 0:
            val = float(g["score"].mean())
        else:
            val = float((g["cw"] * g["score"]).sum() / denom)
        return pd.Series({"team_score": val})

    out = df.groupby(["item","dim"], as_index=False).apply(_agg).reset_index(drop=True)
    return out


def compute_consensus_stats(ratings_long: pd.DataFrame) -> pd.DataFrame:
    """
    For hot-cell detection: range/IQR per item×dim across raters.
    """
    df = ratings_long.copy()

    def _agg(g: pd.DataFrame) -> pd.Series:
        scores = g["score"].to_numpy(dtype=float)
        med = float(np.median(scores))
        rng = float(np.max(scores) - np.min(scores))
        iqr = float(np.subtract(*np.percentile(scores, [75, 25])))
        return pd.Series({"median": med, "range": rng, "IQR": iqr})

    c = df.groupby(["item","dim"], as_index=False).apply(_agg).reset_index(drop=True)
    return c


def mark_hot_cells(ratings_long: pd.DataFrame) -> pd.DataFrame:
    """
    Hot cell = (range>=2) or (IQR>=1) or (max abs delta to median >=2)
    """
    cons = compute_consensus_stats(ratings_long)
    df = ratings_long.merge(cons, on=["item","dim"], how="left")
    df["abs_delta"] = (df["score"] - df["median"]).abs()

    max_abs = df.groupby(["item","dim"], as_index=False)["abs_delta"].max().rename(columns={"abs_delta":"max_abs_delta"})
    cons2 = cons.merge(max_abs, on=["item","dim"], how="left").fillna({"max_abs_delta": 0.0})

    cons2["is_hot"] = (
        (cons2["range"] >= HOTCELL_RULES["range_ge"]) |
        (cons2["IQR"] >= HOTCELL_RULES["iqr_ge"]) |
        (cons2["max_abs_delta"] >= HOTCELL_RULES["abs_delta_ge"])
    )
    return cons2

def build_outlier_events(ratings_long: pd.DataFrame, delta: int = DEFAULT_OUTLIER_DELTA) -> pd.DataFrame:
    """
    Stage-1 (A): outlier events w.r.t. median baseline, per rater×item×dim.
    Outlier if abs(score - median(item,dim)) >= delta.
    """
    df = ratings_long.copy()
    # median baseline per (item,dim)
    med = df.groupby(["item","dim"], as_index=False)["score"].median().rename(columns={"score":"baseline_median"})
    df = df.merge(med, on=["item","dim"], how="left")
    df["deviation"] = df["score"] - df["baseline_median"]
    df["abs_deviation"] = df["deviation"].abs()
    out = df[df["abs_deviation"] >= float(delta)].copy()
    out["delta"] = float(delta)
    # keep only needed columns (+text-like fields if present)
    keep = [c for c in [
        "rater","item","dim","dimension","score","confidence",
        "baseline_median","deviation","abs_deviation","delta",
        "rationale","suggestion"
    ] if c in out.columns]
    return out[keep].reset_index(drop=True)


def _contains_any(s: str, pats: List[str]) -> bool:
    s = (s or "")
    return any((p in s) for p in pats)


def _has_measurement(s: str) -> bool:
    s = (s or "")
    return bool(re.search(r"(\d+(\.\d+)?\s*(ms|s|秒|分钟|小时|%|次|条|个))|(不超过|至少|最多|范围|小于|大于|等于|<=|>=|=)", s))


def _score_evidence(evidence: str, text: str) -> int:
    e = (evidence or "").strip()
    if not e:
        return 0
    if text and e in text:
        return 2
    return 1


def _score_rewrite_exec(rewrite: str) -> int:
    r = (rewrite or "").strip()
    if not r:
        return 0
    # executable rewrite: has normative modal + (condition or measurable/IO)
    has_modal = bool(re.search(r"(必须|应当|应|需|需要|shall|must)", r, flags=re.I))
    has_cond = bool(re.search(r"(当|如果|在.+时|若|when|if)", r, flags=re.I))
    has_meas = _has_measurement(r)
    if has_modal and (has_cond or has_meas):
        return 2
    return 1


def _score_falsifiable(s: str) -> int:
    x = (s or "")
    test_words = ["验收","测试","检查","判断","满足","输出","返回","错误","日志","状态","响应","成功","失败","不超过","至少","最多","准确","延迟","吞吐"]
    if _contains_any(x, test_words):
        return 2 if _has_measurement(x) else 1
    return 0


def _score_specificity(rationale: str) -> int:
    x = (rationale or "")
    if not x.strip():
        return 0
    kws = ["边界","异常","权限","角色","输入","输出","格式","单位","状态","条件","触发","频率","超时","重试","范围","阈值","参数","依赖","优先级","一致性","冲突","前置"]
    hit = sum(1 for k in kws if k in x)
    if hit >= 2:
        return 2
    if hit == 1:
        return 1
    # generic but non-empty rationale
    generic = ["不清晰","歧义","模糊","不明确","缺少","需要补充"]
    return 1 if _contains_any(x, generic) else 0


def _score_taxonomy_fit(dim: str, rationale: str, rewrite: str, evidence: str) -> int:
    x = " ".join([(rationale or ""), (rewrite or ""), (evidence or "")])
    if not x.strip():
        return 0
    target = {
        "U": ["难以理解","术语","定义","描述不清","表述","复杂","可读性"],
        "A": ["歧义","模糊","不明确","指代","范围不明","语义"],
        "C": ["错误","不一致","冲突","逻辑","矛盾","不正确"],
        "V": ["验收","测试","可验证","指标","测量","量化","日志","输出"],
    }.get(dim, [])
    if _contains_any(x, target):
        return 2
    return 1


def compute_outlier_quality(
    outlier_events: pd.DataFrame,
    req_df: pd.DataFrame,
    issues_long: pd.DataFrame,
    tau_good: float = DEFAULT_TAU_GOOD,
    tau_bad: float = DEFAULT_TAU_BAD,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Stage-2 (B) + Stage-3: rule-based argument-quality vectors + auto decision (good/bad/uncertain).
    Returns (quality_table, decisions_table).
    """
    if outlier_events.empty:
        qt = pd.DataFrame(columns=["rater","item","dim","E_evidence","F_falsifiable","R_rewrite_exec","S_specificity","T_taxonomy_fit","D_novelty","Q","hard_fail"])
        dt = pd.DataFrame(columns=["rater","item","dim","decision","Q","hard_fail"])
        return qt, dt

    req_map = dict(zip(req_df["item"], req_df["text"]))
    # evidence/rewrite from issues_long (if provided)
    iss = issues_long.copy()
    if not iss.empty and "type" in iss.columns:
        iss = iss.rename(columns={"type":"dim"})
    iss_key = ["rater","item","dim"]
    iss = iss.drop_duplicates(subset=iss_key, keep="last") if set(iss_key).issubset(iss.columns) else iss
    out = outlier_events.merge(iss[iss_key + [c for c in ["evidence","rewrite"] if c in iss.columns]] if set(iss_key).issubset(iss.columns) else outlier_events.assign(evidence="", rewrite=""),
                              on=iss_key, how="left")

    out["text"] = out["item"].map(req_map).fillna("")
    out["evidence"] = out.get("evidence", "").fillna("")
    out["rewrite"] = out.get("rewrite", "").fillna("")
    out["rewrite2"] = out["rewrite"].where(out["rewrite"].astype(str).str.strip().ne(""), out.get("suggestion","").fillna(""))
    out["rationale2"] = out.get("rationale","").fillna("")

    # novelty: keyword-set uniqueness within (item,dim)
    novelty_kws = ["边界","异常","权限","角色","输入","输出","格式","单位","状态","条件","触发","频率","超时","重试","范围","阈值","参数","一致性","冲突","前置","歧义","模糊","术语","定义","验收","测试","量化","指标"]
    def _kwset(x: str) -> set:
        x = (x or "")
        return {k for k in novelty_kws if k in x}

    grouped = out.groupby(["item","dim"])
    # compute union of others per row via group pre-computation
    union_map = {}
    for (it, dm), g in grouped:
        sets = []
        for _, r in g.iterrows():
            sets.append(_kwset(" ".join([str(r.get("rationale2","")), str(r.get("rewrite2","")), str(r.get("evidence",""))])))
        union_all = set().union(*sets) if sets else set()
        union_map[(it, dm)] = union_all

    E = []
    F = []
    R = []
    S = []
    T = []
    D = []
    for _, r in out.iterrows():
        text = str(r.get("text",""))
        evidence = str(r.get("evidence",""))
        rewrite = str(r.get("rewrite2",""))
        rationale = str(r.get("rationale2",""))
        dim = str(r.get("dim",""))

        e = _score_evidence(evidence, text)
        rr = _score_rewrite_exec(rewrite)
        ff = _score_falsifiable(rewrite + " " + rationale)
        ss = _score_specificity(rationale)
        tt = _score_taxonomy_fit(dim, rationale, rewrite, evidence)

        self_set = _kwset(" ".join([rationale, rewrite, evidence]))
        union_all = union_map.get((str(r.get("item","")), dim), set())
        unique = self_set - (union_all - self_set)
        ucnt = len(unique)
        if not (rationale or rewrite or evidence):
            dd = 0
        elif ucnt >= 2:
            dd = 2
        elif ucnt == 1:
            dd = 1
        else:
            dd = 0

        E.append(e); R.append(rr); F.append(ff); S.append(ss); T.append(tt); D.append(dd)

    out["E_evidence"] = E
    out["F_falsifiable"] = F
    out["R_rewrite_exec"] = R
    out["S_specificity"] = S
    out["T_taxonomy_fit"] = T
    out["D_novelty"] = D

    # Q = mean of normalized sub-scores
    out["Q"] = (out[["E_evidence","F_falsifiable","R_rewrite_exec","S_specificity","T_taxonomy_fit","D_novelty"]].sum(axis=1) / (2.0 * 6.0)).astype(float)
    out["hard_fail"] = (out["E_evidence"] == 0) | (out["R_rewrite_exec"] == 0) | (out["T_taxonomy_fit"] == 0)

    def _dec(row):
        if bool(row["hard_fail"]) or float(row["Q"]) <= float(tau_bad):
            return "bad"
        if (not bool(row["hard_fail"])) and float(row["Q"]) >= float(tau_good):
            return "good"
        return "uncertain"

    out["decision"] = out.apply(_dec, axis=1)

    quality_cols = ["rater","item","dim","abs_deviation","E_evidence","F_falsifiable","R_rewrite_exec","S_specificity","T_taxonomy_fit","D_novelty","Q","hard_fail"]
    dec_cols = ["rater","item","dim","abs_deviation","Q","hard_fail","decision"]
    quality_table = out[quality_cols].copy()
    decisions = out[dec_cols].copy()
    return quality_table.reset_index(drop=True), decisions.reset_index(drop=True)


def compute_weights_from_outliers(
    outlier_quality: pd.DataFrame,
    outlier_decisions: pd.DataFrame,
    raters: List[str],
    prior_weights: Optional[Dict[str, float]] = None,
    lam_bad: float = DEFAULT_LAMBDA_BAD,
    beta: float = DEFAULT_BETA_WEIGHT,
    score_prior: float = DEFAULT_WEIGHT_SCORE_PRIOR,
    evidence_prior: float = DEFAULT_DOC_EVIDENCE_PRIOR,
) -> Tuple[Dict[str,float], pd.DataFrame]:
    """
    Stage-4: prior-guided fixed weights for the current document.
      prior_r = cross-document prior if provided, else uniform
      G_r = sum(abs_dev * Q) over good
      B_r = sum(abs_dev * (1-Q)) over bad
      s_r = (G_r - lam_bad*B_r) / (score_prior + n_good + n_bad)
      rho_doc = total_signal / (total_signal + evidence_prior)
      w_doc = softmax(beta * s_r)
      w_final = (1-rho_doc) * prior + rho_doc * w_doc
    Returns (weights_dict, model_profile_df).
    """
    prior = normalize_weight_map(raters, prior_weights)
    if outlier_decisions.empty or outlier_quality.empty:
        w = prior
        prof = pd.DataFrame({"rater": raters, "G": 0.0, "B": 0.0, "weight_raw": 1.0, "weight": list(w.values()),
                             "n_good": 0, "n_bad": 0, "n_uncertain": 0, "n_outliers": 0})
        return w, prof

    q = outlier_quality.merge(outlier_decisions[["rater","item","dim","decision"]], on=["rater","item","dim"], how="left")
    q["abs"] = q["abs_deviation"].astype(float)
    q["Q"] = q["Q"].astype(float)
    q["decision"] = q["decision"].fillna("uncertain")

    rows = []
    total_signal = 0.0
    for r in raters:
        gr = q[(q["rater"]==r) & (q["decision"]=="good")]
        br = q[(q["rater"]==r) & (q["decision"]=="bad")]
        ur = q[(q["rater"]==r) & (q["decision"]=="uncertain")]
        G = float((gr["abs"] * gr["Q"]).sum()) if not gr.empty else 0.0
        B = float((br["abs"] * (1.0 - br["Q"])).sum()) if not br.empty else 0.0
        decisive = int(len(gr) + len(br))
        total_signal += (G + B)
        score = (G - float(lam_bad) * B) / float(float(score_prior) + decisive) if decisive > 0 else 0.0
        rows.append({
            "rater": r,
            "G": G,
            "B": B,
            "n_good": int(len(gr)),
            "n_bad": int(len(br)),
            "n_uncertain": int(len(ur)),
            "n_outliers": int(len(gr)+len(br)+len(ur)),
            "_score": score,
        })
    prof = pd.DataFrame(rows)
    doc_scores = float(beta) * prof["_score"].to_numpy(dtype=float)
    doc_weights = stable_softmax(doc_scores)
    rho_doc = total_signal / float(total_signal + float(evidence_prior)) if total_signal > 0.0 else 0.0
    prior_vec = np.array([prior[r] for r in prof["rater"]], dtype=float)
    final_weights = (1.0 - rho_doc) * prior_vec + rho_doc * doc_weights
    total = float(final_weights.sum())
    if total <= 0.0 or not np.isfinite(total):
        final_weights = prior_vec
        total = float(final_weights.sum())
    prof["weight_raw"] = doc_weights
    prof["weight"] = final_weights / total if total > 0.0 else prior_vec
    prof = prof.drop(columns=["_score"])
    weights = dict(zip(prof["rater"], prof["weight"]))
    return weights, prof


def build_default_model_profile(raters: List[str], weights: Dict[str, float]) -> pd.DataFrame:
    w = normalize_weight_map(raters, weights)
    rows = []
    for r in raters:
        rows.append({
            "rater": r,
            "G": 0.0,
            "B": 0.0,
            "weight_raw": float(w.get(r, 0.0)),
            "weight": float(w.get(r, 0.0)),
            "n_good": 0,
            "n_bad": 0,
            "n_uncertain": 0,
            "n_outliers": 0,
        })
    return pd.DataFrame(rows, columns=MODEL_PROFILE_COLUMNS)


def augment_missing_good_issue_rows(
    issues_long: pd.DataFrame,
    good_flags: pd.DataFrame,
    ratings_long: pd.DataFrame,
) -> pd.DataFrame:
    if good_flags.empty:
        return issues_long

    have = issues_long[["rater", "item", "type"]].drop_duplicates() if not issues_long.empty else empty_df(["rater", "item", "type"])
    miss = good_flags.merge(have, on=["rater", "item", "type"], how="left", indicator=True)
    miss = miss[miss["_merge"] == "left_only"][["rater", "item", "type"]]
    if miss.empty:
        return issues_long

    sug = ratings_long[["rater", "item", "dim", "suggestion"]].rename(columns={"dim": "type"})
    miss = miss.merge(sug, on=["rater", "item", "type"], how="left")
    miss["dimension"] = miss["type"].map(SHORT_DIM)
    miss["evidence"] = ""
    miss["rewrite"] = miss["suggestion"].fillna("")
    miss = miss[["rater", "item", "type", "dimension", "evidence", "rewrite"]]
    return pd.concat([issues_long, miss], ignore_index=True, sort=False)


def prepare_treatment_context(
    treatment: TreatmentConfig,
    req_df: pd.DataFrame,
    ratings_long: pd.DataFrame,
    issues_long: pd.DataFrame,
    raters: List[str],
    prior_weights: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    weights = normalize_weight_map(raters, None if treatment.force_equal_weights else prior_weights)
    model_profile = build_default_model_profile(raters, weights)
    outlier_events = empty_df(OUTLIER_EVENT_COLUMNS)
    outlier_quality = empty_df(OUTLIER_QUALITY_COLUMNS)
    outlier_decisions = empty_df(OUTLIER_DECISION_COLUMNS)
    q_map = empty_df(Q_MAP_COLUMNS)
    candidate_pairs = empty_df(CANDIDATE_PAIR_COLUMNS)

    if treatment.use_outlier_pipeline:
        outlier_events = build_outlier_events(ratings_long, delta=DEFAULT_OUTLIER_DELTA)
        outlier_quality, outlier_decisions = compute_outlier_quality(
            outlier_events,
            req_df,
            issues_long,
            tau_good=DEFAULT_TAU_GOOD,
            tau_bad=DEFAULT_TAU_BAD,
        )

        if treatment.use_quality_scores and not outlier_quality.empty:
            q_map = outlier_quality[["rater", "item", "dim", "Q"]].rename(columns={"dim": "type"})

        if treatment.recompute_weights:
            weights, model_profile = compute_weights_from_outliers(
                outlier_quality,
                outlier_decisions,
                raters=raters,
                prior_weights=weights,
                lam_bad=DEFAULT_LAMBDA_BAD,
                beta=DEFAULT_BETA_WEIGHT,
            )
        else:
            model_profile = build_default_model_profile(raters, weights)

        if treatment.fixed_candidate_pairs:
            candidate_pairs = (
                outlier_decisions[outlier_decisions["decision"] == "good"][["item", "dim"]]
                .rename(columns={"dim": "type"})
                .drop_duplicates()
            ) if not outlier_decisions.empty else empty_df(CANDIDATE_PAIR_COLUMNS)
            if candidate_pairs.empty:
                candidate_pairs = issues_long[["item", "type"]].drop_duplicates() if not issues_long.empty else empty_df(CANDIDATE_PAIR_COLUMNS)

            good_flags = (
                outlier_decisions[outlier_decisions["decision"] == "good"][["rater", "item", "dim"]]
                .rename(columns={"dim": "type"})
            ) if not outlier_decisions.empty else empty_df(["rater", "item", "type"])
            issues_long = augment_missing_good_issue_rows(issues_long, good_flags, ratings_long)
            if not candidate_pairs.empty and not issues_long.empty:
                issues_long = issues_long.merge(candidate_pairs, on=["item", "type"], how="inner")

    return {
        "weights": weights,
        "model_profile": model_profile,
        "outlier_events": outlier_events,
        "outlier_quality": outlier_quality,
        "outlier_decisions": outlier_decisions,
        "candidate_pairs": candidate_pairs,
        "q_map": q_map,
        "issues_long": issues_long,
    }


# def issue_support_scores(issues_long: pd.DataFrame, ratings_long: pd.DataFrame, weights: Dict[str, float]) -> pd.DataFrame:
def issue_support_scores(issues_long: pd.DataFrame, ratings_long: pd.DataFrame, weights: Dict[str, float],q_map: Optional[pd.DataFrame] = None,flags: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    For each issue candidate (item,type): S = sum_{raters who flagged} w * recalibrated_conf(type)
    """
    # map each rater's confidence for that type
    conf_map = ratings_long[["rater","item","dim","confidence"]].copy()
    conf_map.rename(columns={"dim":"type"}, inplace=True)  # type in U/A/C/V
    conf_map["pc"] = conf_map["confidence"].apply(recalibrate_conf)
    conf_map["w"] = conf_map["rater"].map(weights).fillna(0.0)
    # conf_map["influence"] = conf_map["w"] * conf_map["pc"]

    # argument-quality factor Q (default=1.0 if unknown)
    if q_map is not None and not q_map.empty:
        qm = q_map.copy()
        # expect cols: rater,item,type,Q (or dim)
        if "dim" in qm.columns and "type" not in qm.columns:
            qm = qm.rename(columns={"dim":"type"})
        qm = qm[["rater","item","type","Q"]].drop_duplicates()
        conf_map = conf_map.merge(qm, on=["rater","item","type"], how="left")
        conf_map["Q"] = conf_map["Q"].fillna(1.0).astype(float)
    else:
        conf_map["Q"] = 1.0
    conf_map["influence"] = conf_map["w"] * conf_map["pc"] * conf_map["Q"]


    # only count raters who flagged that issue
    # flagged = issues_long[["rater","item","type"]].drop_duplicates()

    # only count raters who flagged that issue
    if flags is not None and not flags.empty:
        f = flags.copy()
        if "dim" in f.columns and "type" not in f.columns:
            f = f.rename(columns={"dim":"type"})
        flagged = f[["rater","item","type"]].drop_duplicates()
    else:
        flagged = issues_long[["rater","item","type"]].drop_duplicates()

    joined = flagged.merge(conf_map[["rater","item","type","influence"]], on=["rater","item","type"], how="left").fillna({"influence":0.0})

    s = joined.groupby(["item","type"], as_index=False)["influence"].sum().rename(columns={"influence":"S_issue"})
    s["dimension"] = s["type"].map(SHORT_DIM)
    return s.sort_values("S_issue", ascending=False).reset_index(drop=True)


def build_topk_issues_block(
    topk: pd.DataFrame,
    req_df: pd.DataFrame,
    issues_long: pd.DataFrame,
    ratings_long: pd.DataFrame,
    weights: Dict[str, float],
    k: int,
    q_map: Optional[pd.DataFrame] = None
) -> str:
    """
    Build human-readable block for LLM prompt, sorted by S_issue desc, with per-issue supporting views ordered by influence.
    """
    req_map = dict(zip(req_df["item"], req_df["text"]))

    # compute per-rater influence for each issue type
    conf_map = ratings_long[["rater","item","dim","confidence"]].copy()
    conf_map.rename(columns={"dim":"type"}, inplace=True)
    conf_map["pc"] = conf_map["confidence"].apply(recalibrate_conf)
    conf_map["w"] = conf_map["rater"].map(weights).fillna(0.0)
    # conf_map["influence"] = conf_map["w"] * conf_map["pc"]

    # argument-quality factor Q (default=1.0 if unknown)
    if q_map is not None and not q_map.empty:
        qm = q_map.copy()
        if "dim" in qm.columns and "type" not in qm.columns:
            qm = qm.rename(columns={"dim":"type"})
        qm = qm[["rater","item","type","Q"]].drop_duplicates()
        conf_map = conf_map.merge(qm, on=["rater","item","type"], how="left")
        conf_map["Q"] = conf_map["Q"].fillna(1.0).astype(float)
    else:
        conf_map["Q"] = 1.0
    conf_map["influence"] = conf_map["w"] * conf_map["pc"] * conf_map["Q"]

    blocks = []
    used_items = set()
    for _, row in topk.head(k).iterrows():
        item = row["item"]
        typ = row["type"]
        S = float(row["S_issue"])
        used_items.add(item)

        text = req_map.get(item, "")
        # collect raters who flagged this issue
        sub_iss = issues_long[(issues_long["item"] == item) & (issues_long["type"] == typ)].copy()
        if sub_iss.empty:
            continue
        sub_iss = sub_iss.merge(conf_map[["rater","item","type","influence"]], on=["rater","item","type"], how="left").fillna({"influence":0.0})
        sub_iss = sub_iss.sort_values("influence", ascending=False)

        # attach score info for that dim

        dim_scores = ratings_long[(ratings_long["item"] == item) & (ratings_long["dim"] == typ)][
            ["rater", "score", "confidence", "rationale", "suggestion"]
        ].copy()
        # avoid cartesian product: join on (rater,item,type)
        dim_scores["item"] = item
        dim_scores["type"] = typ
        dim_scores = dim_scores.merge(
            conf_map[["rater", "item", "type", "influence"]],
            on = ["rater", "item", "type"],
            how = "left"
        )

        dim_scores = dim_scores.sort_values("influence", ascending=False)

        views = []
        for _, v in dim_scores.iterrows():
            views.append(
                f"- {v['rater']} | score={v['score']}, conf={v['confidence']:.2f}, infl≈{float(v['influence']):.3f}\n"
                f"  rationale: {str(v['rationale'])[:300]}\n"
                f"  suggestion: {str(v['suggestion'])[:200]}"
            )

        ev = []
        for _, it in sub_iss.iterrows():
            ev.append(
                f"- {it['rater']} infl≈{float(it['influence']):.3f}\n"
                f"  evidence: {str(it['evidence'])[:200]}\n"
                f"  rewrite: {str(it['rewrite'])[:200]}"
            )

        blocks.append(
            f"[Issue] item={item} type={typ} (dim={SHORT_DIM[typ]}) S_issue={S:.3f}\n"
            f"Requirement: {text[:600]}\n"
            f"Rater views (ordered by influence):\n" + "\n".join(views[:6]) + "\n"
            f"Flagged issue details:\n" + "\n".join(ev[:6]) + "\n"
        )

    return "\n\n".join(blocks)


# =========================
# 5) Fixed weights: load or compute from analyze report + labels
# =========================
def load_weights_csv(path: str) -> Dict[str, float]:
    df = read_csv_smart(path)
    need = ["rater","weight"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise ValueError(f"{path} missing columns {miss}; got {list(df.columns)}")
    df = df.copy()
    df["rater"] = df["rater"].astype(str).str.strip()
    df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
    df = df.dropna(subset=["weight"])
    s = float(df["weight"].sum())
    if s <= 0:
        raise ValueError("weights sum <=0")
    df["weight"] = df["weight"] / s
    return dict(zip(df["rater"], df["weight"]))


def load_model_labels(path: str) -> pd.DataFrame:
    df = read_csv_smart(path)
    need = ["rater","bad_global","good_global"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise ValueError(f"{path} missing columns {miss}")
    df = df.copy()
    df["rater"] = df["rater"].astype(str).str.strip()
    df["bad_global"] = pd.to_numeric(df["bad_global"], errors="coerce").fillna(0).astype(int)
    df["good_global"] = pd.to_numeric(df["good_global"], errors="coerce").fillna(0).astype(int)
    return df[["rater","bad_global","good_global"]]


def load_hotcell_labels(path: str) -> pd.DataFrame:
    df = read_csv_smart(path)
    need = ["rater","item","dimension","label"]
    miss = [c for c in need if c not in df.columns]
    if miss:
        raise ValueError(f"{path} missing columns {miss}")
    df = df.copy()
    df["rater"] = df["rater"].astype(str).str.strip()
    df["item"] = df["item"].apply(normalize_item)
    df["dimension"] = df["dimension"].astype(str).str.strip()
    df["label"] = df["label"].astype(str).str.lower().str.strip()
    # normalize dimension to U/A/C/V if needed
    # accept "Unambiguous" etc
    dim2 = []
    for x in df["dimension"].tolist():
        if x.upper() in ["U","A","C","V"]:
            dim2.append(x.upper())
        elif x in DIMENSIONS:
            dim2.append(DIM_SHORT[x])
        else:
            # fallback: first letter
            dim2.append(x[:1].upper())
    df["dim"] = dim2
    df["is_bad"] = df["label"].isin(["bad","drop"]).astype(int)
    df["is_good"] = df["label"].isin(["good","keep"]).astype(int)
    return df[["rater","item","dim","is_bad","is_good"]]


def minmax_norm(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    mn = float(np.nanmin(x.to_numpy())) if np.isfinite(np.nanmin(x.to_numpy())) else np.nan
    mx = float(np.nanmax(x.to_numpy())) if np.isfinite(np.nanmax(x.to_numpy())) else np.nan
    if pd.isna(mn) or pd.isna(mx):
        return pd.Series(np.nan, index=s.index)
    if mx == mn:
        return pd.Series(0.0, index=s.index)
    return (x - mn) / (mx - mn)


def compute_weights_from_analyze_report(
    report_xlsx: str,
    raters: List[str],
    model_labels_csv: Optional[str],
    hotcell_labels_csv: Optional[str],
) -> Dict[str, float]:
    """
    Uses sheets from analyze_llm_likert_scores output (req-level):
      - avgdisg_req: avg_distance_to_others
      - loo_req: contribution(G-G_minus)
      - rater_summary_req: bias_to_median (=> bias_abs)
      - dev_summary_req: mean_abs_delta
    """
    xl = pd.ExcelFile(report_xlsx)

    # --- PATCH START: accept both avgdist_req (new) and avgdisg_req (old) ---
    def _pick_sheet(cands):
        for s in cands:
            if s in xl.sheet_names:
                return s
        raise ValueError(f"{report_xlsx} missing sheets {cands}. Found: {xl.sheet_names}")

    avgdist_sh = _pick_sheet(["avgdist_req", "avgdisg_req"])
    # --- PATCH END ---

    required_sheets = ["loo_req", "rater_summary_req", "dev_summary_req"]
    for sh in required_sheets:
        if sh not in xl.sheet_names:
            raise ValueError(f"{report_xlsx} missing sheet '{sh}'. Found: {xl.sheet_names}")

    avgdisg = xl.parse(avgdist_sh)
    loo = xl.parse("loo_req")
    rs = xl.parse("rater_summary_req")
    devs = xl.parse("dev_summary_req")

    # Build table
    df = pd.DataFrame({"rater": raters})
    # avg_distance_to_others
    if "avg_distance_to_others" not in avgdisg.columns:
        raise ValueError("avgdisg_req missing avg_distance_to_others")
    df = df.merge(avgdisg[["rater","avg_distance_to_others"]], on="rater", how="left")

    # loo contribution
    if "contribution(G-G_minus)" not in loo.columns:
        raise ValueError("loo_req missing contribution(G-G_minus)")
    df = df.merge(loo[["rater","contribution(G-G_minus)"]], on="rater", how="left")

    # bias abs
    if "bias_to_median" not in rs.columns:
        raise ValueError("rater_summary_req missing bias_to_median")
    rs2 = rs[["rater","bias_to_median"]].copy()
    rs2["bias_abs"] = rs2["bias_to_median"].abs()
    df = df.merge(rs2[["rater","bias_abs"]], on="rater", how="left")

    # mean abs delta
    if "mean_abs_delta" not in devs.columns:
        raise ValueError("dev_summary_req missing mean_abs_delta")
    df = df.merge(devs[["rater","mean_abs_delta"]], on="rater", how="left")

    # normalize metrics and compute OutlierIndex
    oi = pd.Series(0.0, index=df.index, dtype=float)
    for m, a in OUTLIER_METRICS_WEIGHTS.items():
        df[f"norm_{m}"] = minmax_norm(df[m])
        oi += a * df[f"norm_{m}"].fillna(0.0)
    df["OI"] = oi

    # human labels
    bad_global = pd.Series(0.0, index=df.index)
    if model_labels_csv and os.path.exists(model_labels_csv):
        ml = load_model_labels(model_labels_csv)
        df = df.merge(ml[["rater","bad_global"]], on="rater", how="left")
        bad_global = df["bad_global"].fillna(0.0).astype(float)
    else:
        df["bad_global"] = 0

    bad_hot_rate = pd.Series(0.0, index=df.index)
    good_hot_rate = pd.Series(0.0, index=df.index)
    if hotcell_labels_csv and os.path.exists(hotcell_labels_csv):
        hl = load_hotcell_labels(hotcell_labels_csv)
        agg = hl.groupby("rater", as_index=False).agg(
            bad_hot=("is_bad","sum"),
            good_hot=("is_good","sum"),
            total=("is_bad","count")
        )
        agg["bad_hot_rate"] = agg["bad_hot"] / agg["total"].replace(0, np.nan)
        agg["good_hot_rate"] = agg["good_hot"] / agg["total"].replace(0, np.nan)
        df = df.merge(agg[["rater","bad_hot_rate","good_hot_rate"]], on="rater", how="left")
        bad_hot_rate = df["bad_hot_rate"].fillna(0.0).astype(float)
        good_hot_rate = df["good_hot_rate"].fillna(0.0).astype(float)
    else:
        df["bad_hot_rate"] = 0.0
        df["good_hot_rate"] = 0.0

    # Fixed weight formula (monotonic, interpretable)
    raw = np.exp(-df["OI"].fillna(0.0).to_numpy(dtype=float))
    raw *= np.exp(-LAMBDA_BAD_GLOBAL * bad_global.to_numpy(dtype=float))
    raw *= np.exp(-LAMBDA_BAD_HOT * bad_hot_rate.to_numpy(dtype=float))
    raw *= (1.0 + LAMBDA_GOOD_HOT * good_hot_rate.to_numpy(dtype=float))

    raw = np.maximum(raw, 1e-9)
    w = raw / raw.sum()

    df["weight"] = w
    # normalize again guard
    df["weight"] = df["weight"] / df["weight"].sum()

    return dict(zip(df["rater"], df["weight"]))


# =========================
# 6) Roundtable loop
# =========================
def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    inter = len(a & b)
    uni = len(a | b)
    return inter / uni if uni else 0.0


def team_issues_set(issue_scores: pd.DataFrame, theta: float) -> set:
    s = issue_scores[issue_scores["S_issue"] >= theta][["item","type"]]
    return set(map(tuple, s.to_records(index=False)))


def collect_initial_scoring_outputs(
    raters: List[Rater],
    req_df: pd.DataFrame,
    out_dir: str,
    round0_cache_dir: str = "",
    require_round0_cache: bool = False,
) -> Dict[str, Any]:
    all_ratings = []
    all_issues = []
    disabled_scoring_raters = set()
    raw_logs = []
    cache_hits = 0
    cache_misses = 0
    cache_skips = 0

    if round0_cache_dir:
        ensure_dir(round0_cache_dir)

    for _, rrow in req_df.iterrows():
        item = rrow["item"]
        text = rrow["text"]
        for r in raters:
            if r.name in disabled_scoring_raters:
                continue

            data = None
            source = "live"
            cache_status = "miss"

            if round0_cache_dir:
                cache_status, cached = load_round0_cache_entry(round0_cache_dir, r.name, item)
                if cache_status == "hit":
                    data = cached
                    source = "round0_cache"
                    cache_hits += 1
                elif cache_status == "skip":
                    cache_skips += 1
                    disabled_scoring_raters.add(r.name)
                    raw_logs.append({
                        "stage": "score",
                        "rater": r.name,
                        "item": item,
                        "source": "round0_cache_error",
                        "cache_status": cache_status,
                        "error": cached.get("error", "") if isinstance(cached, dict) else "",
                    })
                    continue

            if data is None:
                cache_misses += 1
                if require_round0_cache:
                    raise RuntimeError(f"Missing round-0 cache for rater={r.name}, item={item}")
                try:
                    data = score_requirement(r, item, text, out_dir=out_dir)
                    if round0_cache_dir:
                        save_round0_cache_payload(round0_cache_dir, r.name, item, data)
                except RuntimeError as e:
                    if is_skippable_rater_error(e):
                        if round0_cache_dir:
                            save_round0_cache_error(round0_cache_dir, r.name, item, str(e))
                        print(f"{e} -> disable rater for this case")
                        disabled_scoring_raters.add(r.name)
                        raw_logs.append({
                            "stage": "score",
                            "rater": r.name,
                            "item": item,
                            "source": "live_error",
                            "cache_status": cache_status,
                            "error": str(e),
                        })
                        continue
                    raise

            raw_logs.append({
                "stage": "score",
                "rater": r.name,
                "item": item,
                "source": source,
                "cache_status": cache_status,
                "raw": data,
            })
            rating_long, issues_long = flatten_scoring_output(r.name, item, data)
            all_ratings.append(rating_long)
            all_issues.append(issues_long)

    if not all_ratings:
        raise RuntimeError("No successful rater outputs were collected for this case.")

    ratings_long = pd.concat(all_ratings, ignore_index=True)
    issues_long = pd.concat(all_issues, ignore_index=True)
    if disabled_scoring_raters:
        ratings_long = ratings_long[~ratings_long["rater"].isin(disabled_scoring_raters)].copy()
        issues_long = issues_long[~issues_long["rater"].isin(disabled_scoring_raters)].copy()
    if ratings_long.empty:
        raise RuntimeError("All collected scoring rows were removed after skipping failed raters.")

    active_raters = [r for r in raters if r.name not in disabled_scoring_raters]
    if not active_raters:
        raise RuntimeError("All raters were skipped during initial scoring for this case.")

    return {
        "ratings_long": ratings_long,
        "issues_long": issues_long,
        "raw_logs": raw_logs,
        "active_raters": active_raters,
        "disabled_scoring_raters": disabled_scoring_raters,
        "round0_cache_stats": {
            "cache_dir": round0_cache_dir,
            "require_cache": bool(require_round0_cache),
            "hits": int(cache_hits),
            "misses": int(cache_misses),
            "skip_markers": int(cache_skips),
        },
    }


def run_roundtable(
    raters: List[Rater],
    req_df: pd.DataFrame,
    weights: Dict[str, float],
    max_rounds: int,
    topk_issues: int,
    theta: float,
    eps_score: float,
    tau_jacc: float,
    out_dir: str,
    treatment: TreatmentConfig,
    round0_cache_dir: str = "",
    require_round0_cache: bool = False,
) -> Dict[str, Any]:
    """
    Returns dict with final tables + history
    """
    req_map = dict(zip(req_df["item"], req_df["text"]))
    ensure_dir(out_dir)

    blocked_discussion_raters = set()
    initial = collect_initial_scoring_outputs(
        raters=raters,
        req_df=req_df,
        out_dir=out_dir,
        round0_cache_dir=round0_cache_dir,
        require_round0_cache=require_round0_cache,
    )
    ratings_long = initial["ratings_long"]
    issues_long = initial["issues_long"]
    raw_logs = initial["raw_logs"]
    active_raters = initial["active_raters"]
    disabled_scoring_raters = initial["disabled_scoring_raters"]
    round0_cache_stats = initial["round0_cache_stats"]

    prep = prepare_treatment_context(
        treatment=treatment,
        req_df=req_df,
        ratings_long=ratings_long,
        issues_long=issues_long,
        raters=[r.name for r in active_raters],
        prior_weights=weights,
    )
    weights = prep["weights"]
    model_profile = prep["model_profile"]
    outlier_events = prep["outlier_events"]
    outlier_quality = prep["outlier_quality"]
    outlier_decisions = prep["outlier_decisions"]
    candidate_pairs = prep["candidate_pairs"]
    q_map = prep["q_map"]
    issues_long = prep["issues_long"]


    # store round 0
    ratings_by_round = {0: ratings_long.copy()}
    issues_by_round = {0: issues_long.copy()}
    issue_scores_by_round = {}
    team_scores_by_round = {}

    # round history
    hist = []

    prev_team_scores = None
    prev_issue_set = None

    # ---------------- iterations
    for t in range(0, max_rounds + 1):
        cur_ratings = ratings_by_round[t]
        cur_issues = issues_by_round[t]

        # compute issue scores and team scores
        # issue_scores = issue_support_scores(cur_issues, cur_ratings, weights)
        issue_scores = issue_support_scores(cur_issues, cur_ratings, weights, q_map=q_map)
        team_scores = weighted_team_score(cur_ratings, weights)

        issue_scores_by_round[t] = issue_scores
        team_scores_by_round[t] = team_scores

        cur_issue_set = team_issues_set(issue_scores, theta)

        # convergence check (skip at t=0)
        score_converged = False
        issue_converged = False
        max_score_diff = None
        jacc = None

        if prev_team_scores is not None:
            merged = team_scores.merge(prev_team_scores, on=["item","dim"], how="inner", suffixes=("_cur","_prev"))
            diffs = (merged["team_score_cur"] - merged["team_score_prev"]).abs()
            max_score_diff = float(diffs.max()) if not diffs.empty else 0.0
            score_converged = (max_score_diff <= eps_score)

        if prev_issue_set is not None:
            jacc = jaccard(cur_issue_set, prev_issue_set)
            issue_converged = (jacc >= tau_jacc)

        hist.append({
            "round": t,
            "n_issue_candidates": int(len(issue_scores)),
            "n_team_issues_ge_theta": int(sum(issue_scores["S_issue"] >= theta)) if len(issue_scores) else 0,
            "score_converged": bool(score_converged),
            "issue_converged": bool(issue_converged),
            "max_score_diff": max_score_diff,
            "issue_jaccard": jacc,
        })

        # stop if any (after at least 1 iteration computed)
        if t > 0 and (score_converged or issue_converged):
            break
        if t == max_rounds:
            break

        # choose topK issue candidates to discuss (any flagged issue, ranked by S_issue)
        topk = issue_scores.head(topk_issues).copy()
        if topk.empty:
            break

        # issues_block = build_topk_issues_block(topk, req_df, cur_issues, cur_ratings, weights, k=topk_issues)
        issues_block = build_topk_issues_block(topk, req_df, cur_issues, cur_ratings, weights, k=topk_issues,q_map=q_map)
        # in discussion: each rater updates only the involved items
        discuss_items = sorted(set(topk["item"].tolist()))
        next_ratings_rows = []
        next_issues_rows = []

        for r in active_raters:
            if r.name in blocked_discussion_raters:
                keep_r = cur_ratings[cur_ratings["rater"]==r.name]
                keep_i = cur_issues[cur_issues["rater"]==r.name]
                next_ratings_rows.append(keep_r.copy())
                next_issues_rows.append(keep_i.copy())
                continue

            msg = [
                {"role":"system", "content": DISCUSS_SYSTEM},
                {"role":"user", "content": DISCUSS_USER_TEMPLATE.format(
                    round_idx=t+1,
                    topk=topk_issues,
                    issues_block=issues_block
                )}
            ]

            try:
                upd = call_llm_json(
                    r, msg,
                    temperature=TEMPERATURE_DISCUSS,
                    out_dir=out_dir,
                    stage="discuss",
                    round_idx=t + 1,
                    item=",".join(discuss_items[:5])  # 只是方便定位，可随便
                )
            except RuntimeError as e:
                if is_skippable_rater_error(e):
                    print(f"{e} -> keep previous state and skip future discussion rounds for this rater")
                    blocked_discussion_raters.add(r.name)
                    keep_r = cur_ratings[cur_ratings["rater"]==r.name]
                    keep_i = cur_issues[cur_issues["rater"]==r.name]
                    next_ratings_rows.append(keep_r.copy())
                    next_issues_rows.append(keep_i.copy())
                    continue
                raise

            raw_logs.append({"stage":"discuss", "round":t+1, "rater":r.name, "raw":upd})

            items_out = upd.get("items", []) or []
            # Build a map for updated items
            upd_map = {}
            for it in items_out:
                ii = normalize_item(it.get("item",""))
                if ii:
                    upd_map[ii] = it

            # For items not provided by rater, keep old state (for discussed items)
            for item in discuss_items:
                if item in upd_map:
                    data = {
                        "scores": upd_map[item].get("scores", {}),
                        "confidences": upd_map[item].get("confidences", {}),
                        "rationales": upd_map[item].get("rationales", {}),
                        "suggestions": upd_map[item].get("suggestions", {}),
                        "issues": upd_map[item].get("issues", []),
                    }
                    # validation + fix keys maybe already U/A/C/V
                    # If model outputs full names, try map
                    if set(data["scores"].keys()) & set(DIM_SHORT.keys()):
                        # convert to U/A/C/V if needed
                        new_scores = {}
                        for k0, v0 in data["scores"].items():
                            if k0 in DIM_SHORT:
                                new_scores[DIM_SHORT[k0]] = v0
                        data["scores"] = new_scores
                    if set(data["confidences"].keys()) & set(DIM_SHORT.keys()):
                        new_conf = {}
                        for k0, v0 in data["confidences"].items():
                            if k0 in DIM_SHORT:
                                new_conf[DIM_SHORT[k0]] = v0
                        data["confidences"] = new_conf

                    try:
                        data = validate_scoring_payload(data, r.name)
                    except RuntimeError as e:
                        if is_skippable_rater_error(e):
                            dump_invalid_output(out_dir, "discuss", r.name, item, data)
                            print(f"{e} -> keep previous state and skip future discussion rounds for this rater")
                            blocked_discussion_raters.add(r.name)
                            keep_r = cur_ratings[(cur_ratings["rater"]==r.name) & (cur_ratings["item"]==item)]
                            keep_i = cur_issues[(cur_issues["rater"]==r.name) & (cur_issues["item"]==item)]
                            next_ratings_rows.append(keep_r.copy())
                            next_issues_rows.append(keep_i.copy())
                            continue
                        raise

                    # Attach item for flatten
                    data2 = {"scores": data["scores"], "confidences": data["confidences"],
                             "rationales": data.get("rationales", {}), "suggestions": data.get("suggestions", {}),
                             "issues": data.get("issues", [])}
                    rl, il = flatten_scoring_output(r.name, item, data2)
                    next_ratings_rows.append(rl)
                    next_issues_rows.append(il)
                else:
                    # keep previous rows for this rater+item
                    keep_r = cur_ratings[(cur_ratings["rater"]==r.name) & (cur_ratings["item"]==item)]
                    keep_i = cur_issues[(cur_issues["rater"]==r.name) & (cur_issues["item"]==item)]
                    next_ratings_rows.append(keep_r.copy())
                    next_issues_rows.append(keep_i.copy())

            # For non-discussed items, keep old state
            other_r = cur_ratings[(cur_ratings["rater"]==r.name) & (~cur_ratings["item"].isin(discuss_items))]
            other_i = cur_issues[(cur_issues["rater"]==r.name) & (~cur_issues["item"].isin(discuss_items))]
            next_ratings_rows.append(other_r.copy())
            next_issues_rows.append(other_i.copy())

        ratings_by_round[t+1] = pd.concat(next_ratings_rows, ignore_index=True)
        issues_by_round[t+1] = pd.concat(next_issues_rows, ignore_index=True)

        # keep only fixed candidates in issues table
        if treatment.fixed_candidate_pairs and not candidate_pairs.empty and not issues_by_round[t + 1].empty:
            issues_by_round[t + 1] = issues_by_round[t + 1].merge(candidate_pairs, on=["item", "type"],how="inner")
        prev_team_scores = team_scores
        prev_issue_set = cur_issue_set

    # choose final round
    final_round = max(ratings_by_round.keys())
    final_ratings = ratings_by_round[final_round]
    final_issues = issues_by_round[final_round]
    final_issue_scores = issue_scores_by_round[final_round]
    final_team_scores = team_scores_by_round[final_round]

    # produce final problems list based on issues score >= theta
    probs = final_issue_scores[final_issue_scores["S_issue"] >= theta].copy()
    probs = probs.merge(req_df, on="item", how="left")

    # pick best evidence/rewrite from highest influence supporter
    # compute supporter influences per (item,type,rater)
    conf_map = final_ratings[["rater","item","dim","confidence"]].copy()
    conf_map.rename(columns={"dim":"type"}, inplace=True)
    conf_map["pc"] = conf_map["confidence"].apply(recalibrate_conf)
    conf_map["w"] = conf_map["rater"].map(weights).fillna(0.0)
    conf_map["influence"] = conf_map["w"] * conf_map["pc"]

    supports = final_issues.merge(conf_map[["rater","item","type","influence"]], on=["rater","item","type"], how="left").fillna({"influence":0.0})
    supports = supports.sort_values("influence", ascending=False)

    best_rows = []
    for (item, typ), g in supports.groupby(["item","type"]):
        g = g.sort_values("influence", ascending=False)
        top = g.iloc[0]
        # also list top supporters
        top_supporters = g.head(5)[["rater","influence"]].to_dict("records")
        best_rows.append({
            "item": item,
            "type": typ,
            "dimension": SHORT_DIM[typ],
            "best_evidence": top["evidence"],
            "best_rewrite": top["rewrite"],
            "top_supporters": json.dumps(top_supporters, ensure_ascii=False),
        })
    best = pd.DataFrame(
        best_rows,
        columns=["item", "type", "dimension", "best_evidence", "best_rewrite", "top_supporters"],
    )

    if best.empty:
        probs["best_evidence"] = ""
        probs["best_rewrite"] = ""
        probs["top_supporters"] = "[]"
    else:
        probs = probs.merge(best, on=["item","type","dimension"], how="left")

    # Also save a run summary (do NOT overwrite call_llm_json()'s raw_logs.jsonl)
    with open(os.path.join(out_dir, "run_summary.jsonl"), "w", encoding="utf-8") as f:
        for rec in raw_logs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    return {
        "treatment": treatment.key,
        "treatment_label": treatment.label,
        "final_round": final_round,
        "weights": weights,
        "model_profile": model_profile,
        "outlier_events": outlier_events,
        "outlier_quality": outlier_quality,
        "outlier_decisions": outlier_decisions,
        "candidate_pairs": candidate_pairs,
        "q_map": q_map,
        "ratings_by_round": ratings_by_round,
        "issues_by_round": issues_by_round,
        "issue_scores_by_round": issue_scores_by_round,
        "team_scores_by_round": team_scores_by_round,
        "history": pd.DataFrame(hist),
        "final_problems": probs.sort_values("S_issue", ascending=False).reset_index(drop=True),
        "run_meta": {
            "treatment": treatment.key,
            "treatment_label": treatment.label,
            "treatment_config": json.dumps(asdict(treatment), ensure_ascii=False),
            "active_raters": json.dumps([r.name for r in active_raters], ensure_ascii=False),
            "disabled_scoring_raters": json.dumps(sorted(disabled_scoring_raters), ensure_ascii=False),
            "blocked_discussion_raters": json.dumps(sorted(blocked_discussion_raters), ensure_ascii=False),
            "round0_cache_dir": str(round0_cache_dir or ""),
            "round0_require_cache": bool(require_round0_cache),
            "round0_cache_hits": int(round0_cache_stats["hits"]),
            "round0_cache_misses": int(round0_cache_stats["misses"]),
            "round0_cache_skip_markers": int(round0_cache_stats["skip_markers"]),
            "max_rounds": int(max_rounds),
            "theta": float(theta),
        },
    }


# =========================
# 7) Main / CLI
# =========================
def load_raters_from_models_json(models_json: str, allow_missing_api_keys: bool = False) -> List[Rater]:
    with open(models_json, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    raters_cfg = cfg.get("raters", [])
    if not raters_cfg:
        raise ValueError("models.json missing raters")
    raters = []
    for rc in raters_cfg:
        name = str(rc["name"]).strip()
        base_url = str(rc["base_url"]).strip()
        model = str(rc["model"]).strip()
        api_key_env = str(rc.get("api_key_env","")).strip()
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not api_key:
            if allow_missing_api_keys:
                raters.append(Rater(name=name, provider=None))
                continue
            raise ValueError(f"Missing API key for rater '{name}'. Set env '{api_key_env}'.")
        prov = OpenAICompatProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=float(rc.get("timeout", DEFAULT_REQUEST_TIMEOUT)),
            min_interval_sec=float(rc.get("min_interval_sec", DEFAULT_MIN_REQUEST_INTERVAL)),
            max_429_retries=int(rc.get("max_429_retries", MAX_429_RETRIES)),
            retry_429_base_sleep=float(rc.get("retry_429_base_sleep", RETRY_429_BASE_SLEEP)),
            retry_429_max_sleep=float(rc.get("retry_429_max_sleep", RETRY_429_MAX_SLEEP)),
            max_timeout_retries=int(rc.get("max_timeout_retries", MAX_TIMEOUT_RETRIES)),
            retry_timeout_base_sleep=float(rc.get("retry_timeout_base_sleep", RETRY_TIMEOUT_BASE_SLEEP)),
            retry_timeout_max_sleep=float(rc.get("retry_timeout_max_sleep", RETRY_TIMEOUT_MAX_SLEEP)),
        )
        raters.append(Rater(name=name, provider=prov))
    return raters


def export_excel(out_xlsx: str, req_df: pd.DataFrame, weights: Dict[str,float], result: Dict[str,Any]):
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        req_df.to_excel(writer, sheet_name="requirements", index=False)

        if "run_meta" in result and isinstance(result["run_meta"], dict):
            meta_df = pd.DataFrame(
                [{"key": k, "value": v} for k, v in result["run_meta"].items()],
                columns=["key", "value"],
            )
            meta_df.to_excel(writer, sheet_name="run_meta", index=False)

        wdf = pd.DataFrame({"rater": list(weights.keys()), "weight": list(weights.values())}).sort_values("weight", ascending=False)
        wdf.to_excel(writer, sheet_name="weights", index=False)

        hist = result["history"]
        hist.to_excel(writer, sheet_name="round_history", index=False)

        # outlier / quality analysis (current document)
        if "model_profile" in result and isinstance(result["model_profile"], pd.DataFrame):
            result["model_profile"].to_excel(writer, sheet_name="model_profile", index=False)
        if "outlier_events" in result and isinstance(result["outlier_events"], pd.DataFrame):
            result["outlier_events"].to_excel(writer, sheet_name="outlier_events", index=False)
        if "outlier_quality" in result and isinstance(result["outlier_quality"], pd.DataFrame):
            result["outlier_quality"].to_excel(writer, sheet_name="outlier_quality", index=False)
        if "outlier_decisions" in result and isinstance(result["outlier_decisions"], pd.DataFrame):
            result["outlier_decisions"].to_excel(writer, sheet_name="outlier_decisions", index=False)
        if "candidate_pairs" in result and isinstance(result["candidate_pairs"], pd.DataFrame):
            result["candidate_pairs"].to_excel(writer, sheet_name="candidates", index=False)

        # rounds
        for t, df in result["ratings_by_round"].items():
            df.to_excel(writer, sheet_name=f"ratings_r{t}", index=False)
        for t, df in result["issues_by_round"].items():
            df.to_excel(writer, sheet_name=f"issues_r{t}", index=False)
        for t, df in result["issue_scores_by_round"].items():
            df.to_excel(writer, sheet_name=f"issue_scores_r{t}", index=False)
        for t, df in result["team_scores_by_round"].items():
            df.to_excel(writer, sheet_name=f"team_scores_r{t}", index=False)

        # final problems
        result["final_problems"].to_excel(writer, sheet_name="final_problems", index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--requirements", required=True, help="requirements.csv with columns item,text")
    ap.add_argument("--models", required=True, help="models.json (OpenAI-compatible endpoints)")
    ap.add_argument("--out", default="roundtable_report.xlsx", help="output Excel file")
    ap.add_argument("--out_dir", default="roundtable_logs", help="output directory for logs")
    ap.add_argument(
        "--treatment",
        default="full_method",
        help="experimental condition: single_llm | equal_weight_aggregation | roundtable_no_weighting | full_method",
    )
    ap.add_argument("--single_rater", default="", help="required with --treatment single_llm when models.json contains multiple raters")
    ap.add_argument("--round0_cache_dir", default="", help="directory for per-rater round-0 scoring cache")
    ap.add_argument("--require_round0_cache", action="store_true", help="fail instead of calling APIs when a round-0 cache entry is missing")
    ap.add_argument("--topk", type=int, default=DEFAULT_TOPK_ISSUES, help="top-K issues to discuss each round")
    ap.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS, help="max rounds")
    ap.add_argument("--theta_ratio", type=float, default=DEFAULT_THETA_RATIO, help="Theta ratio * sum(weights)")

    # convergence
    ap.add_argument("--eps_score", type=float, default=DEFAULT_EPS_SCORE, help="score convergence epsilon")
    ap.add_argument("--tau_jacc", type=float, default=DEFAULT_TAU_JACCARD, help="issue-set convergence Jaccard threshold")

    # weights sources
    ap.add_argument("--weights_csv", default="", help="optional weights.csv (rater,weight)")
    ap.add_argument("--analyze_xlsx", default="", help="optional llm_rating_report.xlsx from analyze_llm_likert_scores")
    ap.add_argument("--model_labels", default="", help="optional model_labels.csv")
    ap.add_argument("--hotcell_labels", default="", help="optional hotcell_labels.csv")

    args = ap.parse_args()

    treatment = resolve_treatment_config(args.treatment)
    req_df = load_requirements_csv(args.requirements)
    allow_missing_api_keys = bool(args.require_round0_cache) and (not treatment.use_roundtable)
    raters = select_treatment_raters(
        load_raters_from_models_json(args.models, allow_missing_api_keys=allow_missing_api_keys),
        treatment=treatment,
        single_rater=args.single_rater,
    )
    rater_names = [r.name for r in raters]

    if treatment.force_equal_weights:
        weights = {r: 1.0 / len(rater_names) for r in rater_names}
    elif args.weights_csv:
        weights = load_weights_csv(args.weights_csv)
    elif args.analyze_xlsx:
        # optional: reuse prior analysis as a fixed prior; run_roundtable will blend it with current-document evidence
        weights = compute_weights_from_analyze_report(
            report_xlsx=args.analyze_xlsx,
            raters=rater_names,
            model_labels_csv=args.model_labels if args.model_labels else None,
            hotcell_labels_csv=args.hotcell_labels if args.hotcell_labels else None,
        )
    else:
        weights = {r: 1.0/len(rater_names) for r in rater_names}

    # normalize weights over present raters
    wsum = sum(weights.get(r, 0.0) for r in rater_names)
    if wsum <= 0:
        raise ValueError("weights sum <=0 over configured raters")
    weights = {r: weights.get(r, 0.0) / wsum for r in rater_names}

    theta = float(args.theta_ratio) * sum(weights.values())  # if normalized -> theta_ratio
    effective_rounds = int(args.rounds) if treatment.use_roundtable else 0
    print(f"Treatment: {treatment.label} ({treatment.key})")
    print(f"Raters: {', '.join(rater_names)}")
    if args.round0_cache_dir:
        print(f"Round-0 cache: {args.round0_cache_dir}")
        print(f"Require cache: {bool(args.require_round0_cache)}")

    result = run_roundtable(
        raters=raters,
        req_df=req_df,
        weights=weights,
        max_rounds=effective_rounds,
        topk_issues=int(args.topk),
        theta=theta,
        eps_score=float(args.eps_score),
        tau_jacc=float(args.tau_jacc),
        out_dir=args.out_dir,
        treatment=treatment,
        round0_cache_dir=args.round0_cache_dir,
        require_round0_cache=bool(args.require_round0_cache),
    )

    export_excel(args.out, req_df, result.get("weights", weights), result)
    print(f"Saved Excel: {args.out}")
    print(f"Saved logs: {args.out_dir}/raw_logs.jsonl")
    print("Final problems (top 10):")
    print(result["final_problems"][["item","type","dimension","S_issue"]].head(10).to_string(index=False))


if __name__ == "__main__":
    main()


# if __name__ == "__main__":
#     for p in [
#         "bad_json_score_deepseek_R1_raw_a1_1768654221768.txt",
#         "bad_json_score_deepseek_R1_raw_a2_1768654272248.txt",
#     ]:
#         with open(p, "r", encoding="utf-8") as f:
#             t = f.read()
#         obj = extract_first_json(t)
#         print(p, "OK", obj.keys())
