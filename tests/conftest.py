import os
from pathlib import Path

import pytest

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

    連同所有 GOVCHECK_* 一起清：任何模組在 import 時若 load_dotenv()，會把開發者本機
    .env（真實端點/模型名…）灌進 os.environ，而 pytest 在跑任何測試「之前」就會 import
    所有測試模組。不清掉的話，測試會讀到本機設定而非設定檔預設值，結果因人而異。
    需要覆寫的測試自己 setenv 即可（本 fixture 在 test body 之前執行，不會蓋掉它們）。
    """
    for k in [key for key in os.environ if key.startswith("GOVCHECK_")] + _LOG_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GOVCHECK_LOG_DIR", str(tmp_path / "logs"))
    reset_logging()
    set_request_id("-")  # contextvar 不隨 test 邊界重置，清掉上個 case 殘留的 request_id
    yield
    reset_logging()
    set_request_id("-")


@pytest.fixture(scope="session")
def official_f02() -> Path:
    if not OFFICIAL_F02.exists():
        pytest.skip("官方 F02 範本不存在（data/original 未連結）")
    return OFFICIAL_F02
