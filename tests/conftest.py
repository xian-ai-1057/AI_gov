from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_F02 = ROOT / "data" / "original" / "附件二：AI-R02-F02 AI風險評鑑.xlsm"


@pytest.fixture(scope="session")
def official_f02() -> Path:
    if not OFFICIAL_F02.exists():
        pytest.skip("官方 F02 範本不存在（data/original 未連結）")
    return OFFICIAL_F02
