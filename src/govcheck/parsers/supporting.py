"""佐證文件解析：FileProcess 服務（HTTP，localhost）的純轉接層。

gov_doc 不再內嵌任何解析程式；所有佐證（pdf/docx/pptx/xlsx/msg/zip/txt…）都以
``POST {fileprocess_url}/api/parse`` 交給既有的 FileProcess 專案解析，本模組只負責：

1. 上傳安全預檢（副檔名白名單、大小、PDF/OLE/zip 簽章、zip-bomb）；
2. 主機核准檢查（url 主機不在 ``approved_model_hosts`` → 不發請求，資料不出本機）；
3. 把回應的 ``chunks`` 對映成可回溯位置的 :class:`SourceUnit`；
4. 把各種失敗對映成 :class:`DocumentParseStatus`，**絕不 raise**、絕不記錄回應內文。

解析器 bug 一律修在 FileProcess，本模組不做任何內容層修補。
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import mimetypes
import os
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml
from pydantic import BaseModel, Field

from govcheck.logging_setup import get_logger
from govcheck.models import (
    DocumentArtifact,
    DocumentParseStatus,
    SourceUnit,
    UsageSummary,
)
from govcheck.runtime_limits import MODEL_CALL_SEMAPHORE

log = get_logger("supporting_parser")

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "parser_config.yaml"
PARSER_NAME = "fileprocess"
DEFAULT_FILEPROCESS_URL = "http://localhost:3322"
READINESS_TIMEOUT_SECONDS = 2.0

_ZIP_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".zip"}
_OLE_EXTENSIONS = {".doc", ".xls", ".ppt", ".msg"}
_IMAGE_SIGNATURES = {
    ".png": (b"\x89PNG",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".bmp": (b"BM",),
    ".tif": (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
}


def parser_source_ref(url: str | None) -> str:
    """``review_run_meta.parser_source_commit`` 用的來源標記：只留主機名，不留完整 url。"""
    host = urlparse(url).hostname if url else None
    return f"{PARSER_NAME}@{host or 'unconfigured'}"


PARSER_SOURCE_REF = parser_source_ref(DEFAULT_FILEPROCESS_URL)


class ParserSettings(BaseModel):
    enabled_formats: set[str] = Field(default_factory=set)
    approved_model_hosts: set[str] = Field(default_factory=lambda: {"localhost", "127.0.0.1"})
    fileprocess_url: str | None = DEFAULT_FILEPROCESS_URL
    fileprocess_timeout: float = 120.0
    max_file_bytes: int = 50 * 1024 * 1024
    # 對單一文件回傳的 chunk 數上限（FileProcess 一頁／一張工作表／一個附件段落＝一個 chunk）。
    max_pages: int = 200
    max_zip_uncompressed_bytes: int = 200 * 1024 * 1024
    max_zip_ratio: float = 100.0

    def normalized_formats(self) -> set[str]:
        return {value if value.startswith(".") else f".{value}" for value in self.enabled_formats}

    def fileprocess_host(self) -> str | None:
        if not self.fileprocess_url:
            return None
        parsed = urlparse(self.fileprocess_url)
        if parsed.scheme not in {"http", "https"}:
            return None
        return parsed.hostname

    def fileprocess_host_is_approved(self) -> bool:
        host = self.fileprocess_host()
        return bool(host) and host in self.approved_model_hosts

    def source_ref(self) -> str:
        return parser_source_ref(self.fileprocess_url)

    def safe_hash(self) -> str:
        data = self.model_dump(mode="json")
        if data.get("fileprocess_url"):
            data["fileprocess_url"] = hashlib.sha256(data["fileprocess_url"].encode()).hexdigest()
        # set 欄位序列化成 list 的順序不穩定（受 PYTHONHASHSEED 影響），
        # 而 sort_keys 只排 key 不排 value；不先排序，同一份設定在不同 process
        # 會算出不同的 parser_config_hash，稽核就無法用它證明兩次審查設定相同。
        for key, value in data.items():
            if isinstance(value, list):
                data[key] = sorted(value)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


@functools.lru_cache(maxsize=1)
def _load_yaml(path: str) -> dict:
    source = Path(path)
    if not source.exists():
        return {}
    with source.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _as_number(env_value: str | None, raw_value, cast, default):
    """env → yaml → 硬碼預設，逐層容錯（比照 llm/config.py 的 _as_number）。

    .env 慣例會留下 `GOVCHECK_XXX=` 這種空值；直接 float("")/int("") 會丟 ValueError，
    而這個呼叫點在 per-document try 之外，會讓整份審查失敗。空值/壞值一律往下一層退。
    """
    for candidate in (env_value, raw_value):
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            continue
        try:
            return cast(candidate)
        except (TypeError, ValueError):
            log.warning("parser setting ignored: not a %s", cast.__name__)
    return default


def _as_text(env_value: str | None, raw_value, default: str | None) -> str | None:
    for candidate in (env_value, raw_value):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return default


def load_parser_settings(path: str | None = None) -> ParserSettings:
    """讀 parser_config.yaml 並以 GOVCHECK_PARSER_* 環境變數覆寫。

    舊版鍵（WORD_STRATEGY / MODEL_HOST / OCR_ENABLED / VLM_ENABLED / TEMP_ROOT / TIMEOUT）
    已無作用，遇到時靜默忽略，不讓留在 .env 的殘值拖垮審查。
    """
    raw = _load_yaml(str(Path(path) if path else CONFIG_PATH))
    formats_env = os.environ.get("GOVCHECK_PARSER_ENABLED_FORMATS")
    formats = (
        {part.strip().casefold() for part in formats_env.split(",") if part.strip()}
        if formats_env is not None
        else set(raw.get("enabled_formats") or [])
    )
    hosts_env = os.environ.get("GOVCHECK_PARSER_APPROVED_MODEL_HOSTS")
    hosts = (
        {part.strip() for part in hosts_env.split(",") if part.strip()}
        if hosts_env is not None
        else set(raw.get("approved_model_hosts") or ["localhost", "127.0.0.1"])
    )
    return ParserSettings(
        enabled_formats=formats,
        approved_model_hosts=hosts,
        fileprocess_url=_as_text(
            os.environ.get("GOVCHECK_PARSER_FILEPROCESS_URL"),
            raw.get("fileprocess_url"),
            DEFAULT_FILEPROCESS_URL,
        ),
        fileprocess_timeout=_as_number(
            os.environ.get("GOVCHECK_PARSER_FILEPROCESS_TIMEOUT"),
            raw.get("fileprocess_timeout"), float, 120.0),
        max_file_bytes=_as_number(
            os.environ.get("GOVCHECK_PARSER_MAX_FILE_BYTES"), raw.get("max_file_bytes"),
            int, 50 * 1024 * 1024),
        max_pages=_as_number(
            os.environ.get("GOVCHECK_PARSER_MAX_PAGES"), raw.get("max_pages"), int, 200),
        max_zip_uncompressed_bytes=_as_number(
            None, raw.get("max_zip_uncompressed_bytes"), int, 200 * 1024 * 1024),
        max_zip_ratio=_as_number(None, raw.get("max_zip_ratio"), float, 100.0),
    )


def fileprocess_readiness(settings: ParserSettings | None = None) -> dict:
    """``GET {url}/health``（2 秒逾時）；給 ``/api/readiness`` 用，絕不 raise。"""
    settings = settings or load_parser_settings()
    approved = settings.fileprocess_host_is_approved()
    reachable = False
    if approved:
        try:
            response = requests.get(
                f"{settings.fileprocess_url.rstrip('/')}/health",
                timeout=READINESS_TIMEOUT_SECONDS,
            )
            reachable = response.status_code == 200
        except Exception as exc:  # noqa: BLE001 - readiness 只回報，不失敗
            log.info("fileprocess health check failed err=%s", type(exc).__name__)
    return {"backend": PARSER_NAME, "url_host_approved": approved, "reachable": reachable}


class TsParserAdapter:
    """唯一可以把佐證送出程序的元件；目的地限定 ``approved_model_hosts``。"""

    def __init__(self, settings: ParserSettings | None = None) -> None:
        self.settings = settings or load_parser_settings()

    def parse(self, path: str | Path) -> tuple[DocumentArtifact, UsageSummary]:
        source = Path(path)
        usage = UsageSummary()
        filename_hash = hashlib.sha256(source.name.encode()).hexdigest()[:16]
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        config_hash = self.settings.safe_hash()
        document_id = hashlib.sha256(str(source).encode()).hexdigest()[:20]

        def _artifact(status: DocumentParseStatus, warnings: list[str],
                      units: list[SourceUnit] | None = None) -> DocumentArtifact:
            return self._artifact(
                document_id, filename_hash, media_type, config_hash, status, warnings, units,
            )

        try:
            raw = source.read_bytes()
            document_id = hashlib.sha256(raw).hexdigest()[:20]
            ext = source.suffix.casefold()
            self._preflight(raw, ext)
        except _Unsupported as exc:
            log.info("supporting parse skipped document=%s reason=%s", document_id, exc.code)
            return _artifact(DocumentParseStatus.UNSUPPORTED, [exc.code]), usage
        except Exception as exc:  # noqa: BLE001 - 預檢失敗只影響該文件
            log.warning("supporting preflight failed document=%s err=%s",
                        document_id, type(exc).__name__)
            return _artifact(DocumentParseStatus.UNREADABLE, [type(exc).__name__]), usage

        if not self.settings.fileprocess_host_is_approved():
            # fail closed：目的地不在核准清單就不發請求，佐證內容不出本機。
            log.warning("fileprocess host not approved; document=%s not sent", document_id)
            return _artifact(
                DocumentParseStatus.PARSER_UNAVAILABLE, ["fileprocess_host_not_approved"],
            ), usage

        # 只送不透明檔名：原始檔名可能含系統名／單位／人名，不外傳。
        payload = {
            "filename": f"{document_id}{ext}",
            "file_content": base64.b64encode(raw).decode("ascii"),
            "metadata": {},
        }
        url = f"{self.settings.fileprocess_url.rstrip('/')}/api/parse"
        usage.parser_remote_calls += 1
        try:
            with MODEL_CALL_SEMAPHORE:
                response = requests.post(
                    url, json=payload, timeout=self.settings.fileprocess_timeout,
                )
        except Exception as exc:  # noqa: BLE001 - 連線錯誤／逾時皆為服務不可用
            log.warning("fileprocess request failed document=%s err=%s",
                        document_id, type(exc).__name__)
            return _artifact(
                DocumentParseStatus.PARSER_UNAVAILABLE, ["fileprocess_unavailable"],
            ), usage

        status_code = getattr(response, "status_code", None)
        if status_code != 200:
            log.warning("fileprocess rejected document=%s status=%s", document_id, status_code)
            return _artifact(*self._map_error_status(status_code)), usage

        try:
            units = self._normalize_units(document_id, response.json())
        except Exception as exc:  # noqa: BLE001 - 回應格式不符也不記內文
            log.warning("fileprocess contract mismatch document=%s err=%s",
                        document_id, type(exc).__name__)
            return _artifact(
                DocumentParseStatus.UNREADABLE, ["fileprocess_contract_mismatch"],
            ), usage

        if not units or not any(unit.content.strip() for unit in units):
            return _artifact(
                DocumentParseStatus.UNREADABLE, ["empty_parser_output"], units,
            ), usage
        if len(units) > self.settings.max_pages:
            return _artifact(DocumentParseStatus.UNREADABLE, ["page_limit_exceeded"]), usage
        log.debug("supporting parsed document=%s units=%d", document_id, len(units))
        return _artifact(DocumentParseStatus.PARSED, [], units), usage

    def parse_many(self, paths: list[str | Path]) -> tuple[list[DocumentArtifact], UsageSummary]:
        artifacts: list[DocumentArtifact] = []
        usage = UsageSummary()
        seen: set[str] = set()
        for path in paths:
            # dedup 必須發生在解析「之前」：內容雜湊只需重讀一次檔案（多半已在
            # page cache），而重複送 FileProcess 解析是整條管線最貴的動作。
            key = _content_key(path)
            if key is not None and key in seen:
                usage.cache_hits += 1
                continue
            artifact, item_usage = self.parse(path)
            usage.add(item_usage)
            if artifact.document_id in seen:
                usage.cache_hits += 1
                continue
            seen.add(artifact.document_id)
            artifacts.append(artifact)
        return artifacts, usage

    def _preflight(self, raw: bytes, ext: str) -> None:
        if ext not in self.settings.normalized_formats():
            raise _Unsupported("format_disabled")
        if not raw:
            raise ValueError("empty file")
        if len(raw) > self.settings.max_file_bytes:
            raise _Unsupported("file_too_large")
        if ext == ".pdf" and not raw.startswith(b"%PDF"):
            raise ValueError("invalid pdf signature")
        if ext in _ZIP_EXTENSIONS:
            if not raw.startswith(b"PK"):
                raise ValueError("invalid zip signature")
            self._check_zip(raw)
        if ext in _OLE_EXTENSIONS and not raw.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise ValueError("invalid ole signature")
        if ext in _IMAGE_SIGNATURES and not any(
            raw.startswith(sig) for sig in _IMAGE_SIGNATURES[ext]
        ):
            raise ValueError("invalid image signature")

    def _check_zip(self, raw: bytes) -> None:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            total = sum(info.file_size for info in archive.infolist())
            compressed = sum(max(info.compress_size, 1) for info in archive.infolist())
        if total > self.settings.max_zip_uncompressed_bytes:
            raise _Unsupported("zip_uncompressed_limit_exceeded")
        if total / max(compressed, 1) > self.settings.max_zip_ratio:
            raise _Unsupported("zip_ratio_limit_exceeded")

    @staticmethod
    def _map_error_status(status_code: int | None) -> tuple[DocumentParseStatus, list[str]]:
        if status_code == 415:
            return DocumentParseStatus.UNSUPPORTED, ["parser_not_supported"]
        if status_code in {400, 413, 422}:
            return DocumentParseStatus.UNREADABLE, ["fileprocess_rejected"]
        # 5xx、非預期狀態碼：視為服務不可用，讓規則審查照常、佐證改人工覆核。
        return DocumentParseStatus.PARSER_UNAVAILABLE, ["fileprocess_unavailable"]

    @staticmethod
    def _normalize_units(document_id: str, body: object) -> list[SourceUnit]:
        """FileProcess 契約：``{"filename": str, "chunks": [{content, page, filename, from_file,
        is_attachment?}]}``；``is_attachment`` 缺席時以 ``from_file != filename`` 推得。"""
        if not isinstance(body, dict) or not isinstance(body.get("chunks"), list):
            raise ValueError("fileprocess response contract mismatch")
        units: list[SourceUnit] = []
        for index, chunk in enumerate(body["chunks"], start=1):
            if not isinstance(chunk, dict) or not isinstance(chunk.get("content"), str):
                raise ValueError("fileprocess chunk contract mismatch")
            chunk_filename = chunk.get("filename")
            from_file = chunk.get("from_file")
            is_attachment = chunk.get("is_attachment")
            if not isinstance(is_attachment, bool):
                is_attachment = bool(chunk_filename) and chunk_filename != from_file
            page = chunk.get("page")
            locator = str(page) if isinstance(page, int) and not isinstance(page, bool) else str(index)
            units.append(SourceUnit(
                unit_id=f"{document_id}:{index}",
                content=chunk["content"],
                locator_type="attachment" if is_attachment else "page",
                locator_value=locator,
                attachment_path=str(chunk_filename) if is_attachment and chunk_filename else None,
            ))
        return units

    def _artifact(
        self,
        document_id: str,
        filename_hash: str,
        media_type: str,
        config_hash: str,
        status: DocumentParseStatus,
        warnings: list[str],
        units: list[SourceUnit] | None = None,
    ) -> DocumentArtifact:
        return DocumentArtifact(
            document_id=document_id,
            filename_hash=filename_hash,
            media_type=media_type,
            parser_name=PARSER_NAME,
            parser_source_commit=self.settings.source_ref(),
            parser_config_hash=config_hash,
            status=status,
            source_units=units or [],
            warnings=warnings,
        )


def _content_key(path: str | Path) -> str | None:
    """與 TsParserAdapter.parse 的 document_id 同演算法；讀不到檔就回 None 交給 parse 處理。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()[:20]


class _Unsupported(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)
