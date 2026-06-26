"""檢查的共同介面。

任何檢查（規則式或 LLM 判讀）都實作 Check：吃一個表單模型，吐 list[Finding]。
engine 只負責依序跑已註冊的 Check，因此新增 Phase = 新增 Check + 註冊，不動既有程式。
"""

from __future__ import annotations

from typing import Protocol

from govcheck.models import Finding


class Check(Protocol):
    """所有檢查的協定。實作可以是函式或具 __call__ 的類別。"""

    def __call__(self, form) -> list[Finding]:  # noqa: ANN001 - form 型別依表單而定
        ...
