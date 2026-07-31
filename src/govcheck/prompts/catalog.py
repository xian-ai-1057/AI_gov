"""Prompt identifiers and variable contracts; no production prompt text lives here."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptContract:
    prompt_id: str
    required_variables: frozenset[str]
    schema_version: str
    profiles: frozenset[str]
    minimum_app_version: str = "0.2.0"


_ITEM_PROFILES = {
    "1-1": "fairness",
    "3-1": "fairness",
    "3-2": "fairness",
    "4-1": "fairness",
    "1-2": "transparency",
    "1-3": "transparency",
    "1-4": "transparency",
    "4-4": "transparency",
    "4-5": "transparency",
    "1-5": "human_oversight",
    "1-6": "human_oversight",
    "2-1": "business_scope",
    "3-3": "privacy",
    "2-2": "security",
    "3-4": "security",
    "4-2": "security",
    "4-3": "security",
    "5-1": "security",
    "5-2": "security",
    "5-3": "security",
}


# 檢核表 20 個標準項次的顯示短名。這些是 UI 標籤，不是模型判準；與 profile 路由一樣
# 由項次決定，避免提案單位改寫 D 欄後，報告列首也跟著變成難以辨識的長句。
_ITEM_SHORT_NAMES = {
    "1-1": "適法性評估",
    "1-2": "模型決策說明",
    "1-3": "模型測試與驗測結果",
    "1-4": "溝通與救濟機制",
    "1-5": "人工監督與介入",
    "1-6": "定期檢視與簽核",
    "2-1": "業務目標、用途與預期產出",
    "2-2": "委外風險管理",
    "3-1": "資料來源代表性",
    "3-2": "資料蒐集方式與偏差",
    "3-3": "個資保護措施",
    "3-4": "資料取得、傳輸與儲存",
    "4-1": "個人屬性與公平性評估",
    "4-2": "模型監控機制",
    "4-3": "KPI 與偏移監測",
    "4-4": "模型紀錄與版本留存",
    "4-5": "模型挑選與超參數",
    "5-1": "變更管理流程",
    "5-2": "權限設定與帳號管理",
    "5-3": "資訊安全保護與存取程序",
}


def short_name_for_item(item_id: str) -> str | None:
    """Return the canonical display label; unknown template items use the D-column fallback."""
    return _ITEM_SHORT_NAMES.get(item_id)


def profile_for_item(item_id: str) -> str:
    """Return the versioned policy route; never infer from uploaded topic text."""
    try:
        return _ITEM_PROFILES[item_id]
    except KeyError as exc:
        raise KeyError(f"F03 item_id 尚未設定 review profile：{item_id}") from exc


class PromptCatalog:
    COMMON_VARIABLES = frozenset({
        "workflow_stage",
        "items_json",
        "evidence_json",
        "policy_version",
        "output_schema_version",
    })

    def __init__(self) -> None:
        self._contracts = {
            f"govcheck.f03.{profile}": PromptContract(
                prompt_id=f"govcheck.f03.{profile}",
                required_variables=self.COMMON_VARIABLES,
                schema_version="f03-assessment-v2",
                profiles=frozenset({profile}),
            )
            for profile in (
                "fairness",
                "transparency",
                "human_oversight",
                "business_scope",
                "privacy",
                "security",
            )
        }
        self._contracts["govcheck.f03.escalation"] = PromptContract(
            prompt_id="govcheck.f03.escalation",
            required_variables=self.COMMON_VARIABLES | frozenset({"primary_assessments_json"}),
            schema_version="f03-assessment-v2",
            profiles=frozenset({"escalation"}),
        )

    def get(self, prompt_id: str) -> PromptContract:
        try:
            return self._contracts[prompt_id]
        except KeyError as exc:
            raise KeyError(f"Prompt ID 未登錄：{prompt_id}") from exc

    def required_prompt_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._contracts))
