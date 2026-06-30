from pathlib import Path

import pytest

from govcheck.logging_setup import reset_logging

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_F02 = ROOT / "data" / "original" / "附件二：AI-R02-F02 AI風險評鑑.xlsm"

_LOG_ENV_KEYS = [
    "GOVCHECK_LOG_PROFILE", "GOVCHECK_LOG_LEVEL", "GOVCHECK_LOG_CONSOLE",
    "GOVCHECK_LOG_MAX_BYTES", "GOVCHECK_LOG_BACKUP_COUNT", "GOVCHECK_OPERATOR",
]


@pytest.fixture(autouse=True)
def _isolate_logging(monkeypatch, tmp_path):
    """把 log 導向暫存目錄、清環境變數、每個 case 前後重置 logger，避免污染真實 logs/。"""
    for k in _LOG_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GOVCHECK_LOG_DIR", str(tmp_path / "logs"))
    reset_logging()
    yield
    reset_logging()


@pytest.fixture(scope="session")
def official_f02() -> Path:
    if not OFFICIAL_F02.exists():
        pytest.skip("官方 F02 範本不存在（data/original 未連結）")
    return OFFICIAL_F02
