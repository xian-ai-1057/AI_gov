import base64
import os
from pathlib import Path

import pytest
import requests

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


# ── 佐證解析：FileProcess 服務的假端點 ───────────────────────────────────
#
# gov_doc 不再內嵌任何解析程式；佐證一律以 HTTP 交給 FileProcess（localhost:3322）。
# 測試不得打真服務，所以在 requests 模組層面替換 post/get（與 tests/test_llm_client.py 同法），
# `govcheck.parsers.supporting` 透過 `requests.post(...)` 呼叫時自然落到這裡。

_FAKE_TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".tsv"}
_FAKE_BINARY_EXTENSIONS = {".pdf", ".docx", ".pptx", ".msg", ".xlsx", ".zip", ".doc", ".ppt"}


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeFileProcess:
    """可注入的假 FileProcess。

    預設行為依副檔名回 chunks：純文字類回解碼後的檔案內容；pdf/docx/pptx/msg/xlsx/zip 回一段
    固定文字「佐證內容（{ext}）」；其他副檔名回 415。測試可覆寫：
      fake.status = 422                          # 強制回此狀態碼（不看副檔名）
      fake.raise_exc = requests.ConnectionError() # post 時直接拋例外
      fake.chunks = [{"content": ..., "page": 1, "filename": ..., "from_file": ...}]
      fake.health_status = 503 / fake.health_exc = requests.ConnectionError()
    `fake.calls` 記錄每次 POST 的 json（不含 base64 內文，另附解碼後的位元組數 `size`）；
    `fake.health_calls` 記錄 GET /health 的 url。
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.raise_exc: BaseException | None = None
        self.chunks: list[dict] | None = None
        self.calls: list[dict] = []
        self.health_status: int = 200
        self.health_exc: BaseException | None = None
        self.health_calls: list[str] = []

    # requests.post(url, json=..., timeout=...)
    def post(self, url, json=None, timeout=None, **kwargs):
        if self.raise_exc is not None:
            raise self.raise_exc
        payload = dict(json or {})
        encoded = payload.pop("file_content", "")
        raw = base64.b64decode(encoded) if encoded else b""
        record = {"url": url, "timeout": timeout, "size": len(raw), **payload}
        self.calls.append(record)
        filename = str(payload.get("filename", ""))
        if self.status is not None:
            return FakeResponse(self.status, {"detail": "FakeFileProcess"}, "FakeFileProcess")
        if self.chunks is not None:
            return FakeResponse(200, {"filename": filename, "chunks": list(self.chunks)})
        ext = Path(filename).suffix.casefold()
        if ext in _FAKE_TEXT_EXTENSIONS:
            content = raw.decode("utf-8", errors="replace")
        elif ext in _FAKE_BINARY_EXTENSIONS:
            content = f"佐證內容（{ext}）"
        else:
            return FakeResponse(415, {"detail": "UnsupportedFileError"}, "UnsupportedFileError")
        return FakeResponse(200, {
            "filename": filename,
            "chunks": [{
                "content": content,
                "page": 1,
                "filename": filename,
                "from_file": filename,
                "is_attachment": False,
            }],
        })

    # requests.get(url, timeout=...)
    def get(self, url, timeout=None, **kwargs):
        if str(url).rstrip("/").endswith("/health"):
            self.health_calls.append(str(url))
            if self.health_exc is not None:
                raise self.health_exc
            return FakeResponse(self.health_status, {"status": "healthy"})
        # 測試裡任何其他 GET 都不該真的出網
        raise requests.ConnectionError(f"blocked in tests: {url}")


@pytest.fixture
def fake_fileprocess(monkeypatch) -> FakeFileProcess:
    """替換 requests.post / requests.get 為假 FileProcess；回傳可注入行為的 fake 物件。"""
    fake = FakeFileProcess()
    monkeypatch.setattr(requests, "post", fake.post)
    monkeypatch.setattr(requests, "get", fake.get)
    return fake


def pytest_collection_modifyitems(items):
    """需要 data/original 官方範本的測試自動掛上 ``local_data`` marker。

    守則要求這類測試用 marker 標示並在缺檔時乾淨 skip。marker 由 skipif 的 reason
    推導而不是手動掛，否則新增一個用 fixture_builder 的測試時就會忘記——
    pyproject 宣告了這個 marker 卻一直沒有任何測試使用，正是手動掛的下場。
    """
    for item in items:
        for mark in item.iter_markers(name="skipif"):
            if "data/original" in str(mark.kwargs.get("reason", "")):
                item.add_marker(pytest.mark.local_data)
                break
