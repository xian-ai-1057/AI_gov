"""LLM 判讀基礎設施（端點設定 + requests 客戶端）。

與 checks/llm/ 分工：本套件只負責「怎麼呼叫端點」（設定載入、HTTP、JSON 解析），
不含任何治理判讀邏輯；判讀邏輯放 checks/llm/。資料只送至設定端點，不外送公有雲。
"""

from govcheck.llm.client import ChatClient, LLMError, parse_json_object
from govcheck.llm.config import load_llm_config

__all__ = ["ChatClient", "LLMError", "load_llm_config", "parse_json_object"]
