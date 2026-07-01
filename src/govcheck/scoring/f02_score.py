"""F02 固有風險計分引擎：用 config/f02_scoring.yaml 還原 Excel 公式。

公式鏈（對照 .xlsm「AI系統固有風險分級評估表」）：
  1. 每題答到 score_on（多為 Y，反向題為 N）→ 四個風險域各加一筆分數。
  2. 加成前總分 = 各域 SUM。
  3. 加成因子 1/2/3：答「有」(Y) 取乘數，否則 ×1；加成後總分 = 總分 × 三乘數。
  4. 加成後百分比 = 加成後總分 ÷ 正規化分母 × 100。
  5. 本系統風險分數 = 四域百分比的 MAX；分級 = <50 低 / <75 中 / 否則 高。
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

from govcheck.models import DomainScores, F02Form

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "f02_scoring.yaml"


@functools.lru_cache(maxsize=1)
def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_PATH
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class F02ScoreResult:
    """重算結果，含中間量，方便比對與除錯。"""

    def __init__(self, pre_uplift: DomainScores, percentages: DomainScores,
                 overall: float, grade: str):
        self.pre_uplift = pre_uplift
        self.percentages = percentages
        self.overall = overall
        self.grade = grade


def grade_for(overall: float, cfg: dict) -> str:
    t = cfg["grade_thresholds"]
    if overall < t["low_below"]:
        return "低"
    if overall < t["mid_below"]:
        return "中"
    return "高"


def recompute(form: F02Form, cfg: dict | None = None) -> F02ScoreResult:
    cfg = cfg or load_config()
    domains = cfg["domains"]
    questions = cfg["questions"]

    # 1+2. 各域加成前總分
    pre = {d: 0.0 for d in domains}
    for qid, spec in questions.items():
        ans = form.answers.get(qid)
        if ans is not None and ans == spec["score_on"]:
            for d in domains:
                pre[d] += float(spec["scores"][d])

    # 3. 加成因子（答 Y=「有」取乘數，否則 ×1）；直接以 .items() 取鍵，不用 identity 比較
    multiplier = 1.0
    for name, factor in cfg["uplift_factors"].items():
        ans = form.uplift.get(name)
        multiplier *= float(factor["multiplier_on_yes"]) if ans == "Y" else 1.0
    post = {d: pre[d] * multiplier for d in domains}

    # 4. 百分比
    denom = cfg["normalization_denominators"]
    for d in domains:
        if not float(denom[d]):
            raise ValueError(
                f"f02_scoring.yaml 正規化分母 '{d}' 為 0，"
                "請重新執行 scripts/extract_f02_scoring.py（須在 Excel 已試算後的 .xlsm）。"
            )
    pct = {d: (post[d] / float(denom[d])) * 100 for d in domains}

    # 5. MAX → 分級
    overall = max(pct.values())
    return F02ScoreResult(
        pre_uplift=DomainScores(**pre),
        percentages=DomainScores(**pct),
        overall=overall,
        grade=grade_for(overall, cfg),
    )


