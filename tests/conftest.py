import os
from pathlib import Path

import pytest

from govcheck.checks.llm import f03_evidence, f03_rag
from govcheck.logging_setup import reset_logging, set_request_id

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_F02 = ROOT / "data" / "original" / "附件二：AI-R02-F02 AI風險評鑑.xlsm"

_LOG_ENV_KEYS = [
    "GOVCHECK_LOG_PROFILE", "GOVCHECK_LOG_LEVEL", "GOVCHECK_LOG_CONSOLE",
    "GOVCHECK_LOG_MAX_BYTES", "GOVCHECK_LOG_BACKUP_COUNT", "GOVCHECK_OPERATOR",
]


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch, tmp_path):
    """把 log 導向暫存目錄、清環境變數、每個 case 前後重置 logger，避免污染真實 logs/。

    連同所有 GOVCHECK_* 一起清：`scripts/build_regulation_index` 之類的模組在 import 時
    會 load_dotenv()，把開發者本機 .env（GOVCHECK_RAG_ENABLED、真實端點/模型名…）灌進
    os.environ，而 pytest 在跑任何測試「之前」就會 import 所有測試模組。不清掉的話，
    測試會讀到本機設定而非設定檔預設值，結果因人而異。需要覆寫的測試自己 setenv 即可
    （本 fixture 在 test body 之前執行，不會蓋掉它們）。
    """
    for k in [key for key in os.environ if key.startswith("GOVCHECK_")] + _LOG_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GOVCHECK_LOG_DIR", str(tmp_path / "logs"))
    reset_logging()
    set_request_id("-")  # contextvar 不隨 test 邊界重置，清掉上個 case 殘留的 request_id
    yield
    reset_logging()
    set_request_id("-")


@pytest.fixture(autouse=True)
def _legacy_prompt_instructions(monkeypatch):
    """舊版 f03_evidence / f03_rag 在正式環境 fail closed（無 Prompt 來源就不呼叫端點）。

    測試要驗證的是「回應解析與降級邏輯」，故注入一段合成指示把判讀路徑打開，
    等同新版 assessment 測試用 StaticPromptProvider 的作法。正式程式碼的常數維持空字串。
    """
    synthetic = "（測試用合成指示：僅輸出符合指定格式的 JSON）"
    monkeypatch.setattr(f03_evidence, "LEGACY_INSTRUCTIONS", synthetic)
    monkeypatch.setattr(f03_rag, "LEGACY_INSTRUCTIONS", synthetic)


@pytest.fixture(scope="session")
def official_f02() -> Path:
    if not OFFICIAL_F02.exists():
        pytest.skip("官方 F02 範本不存在（data/original 未連結）")
    return OFFICIAL_F02
