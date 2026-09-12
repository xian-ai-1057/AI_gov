"""提案者協填：依佐證文件產出「填寫建議書」（欄位 → 建議 → 出處 → 信心）。

不代填 Excel、不匯出表單；引用只准來自 allow-list，對不上即降為未涵蓋。
"""

from govcheck.assist.engine import suggest_prefill
from govcheck.assist.fields import FieldSpec, load_form_fields
from govcheck.assist.models import AssistResult, Suggestion

__all__ = ["AssistResult", "FieldSpec", "Suggestion", "load_form_fields", "suggest_prefill"]
