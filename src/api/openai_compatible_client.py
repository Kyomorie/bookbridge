"""Client for OpenAI and OpenAI-compatible LLM servers.

Implements the same tiny surface as OllamaClient so existing LLM-assisted
features can switch providers without changing their matching/alignment logic.
"""

import json
import logging
import os
import time
from typing import List, Optional

import requests

from src.api.llm_settings import llm_setting_truthy

logger = logging.getLogger(__name__)

_JUDGE_MAX_TOKENS = 200


def _env_first(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _provider() -> str:
    return (os.environ.get("LLM_PROVIDER") or "ollama").strip().lower()


class OpenAICompatibleClient:
    """Thin OpenAI-compatible HTTP client for chat judging and embeddings."""

    def __init__(self, provider: str = None):
        self.session = requests.Session()
        self.provider = (provider or _provider()).strip().lower()
        self._schema_format_unsupported = False
        self._json_object_unsupported = False

    @property
    def base_url(self) -> str:
        if self.provider == "openai":
            return _env_first("OPENAI_BASE_URL", default="https://api.openai.com/v1").rstrip("/")
        return _env_first("LLM_BASE_URL", default="").rstrip("/")

    @property
    def api_key(self) -> str:
        if self.provider == "openai":
            return _env_first("OPENAI_API_KEY", "LLM_API_KEY", default="")
        return _env_first("LLM_API_KEY", default="")

    @property
    def embed_model(self) -> str:
        if self.provider == "openai":
            return _env_first("LLM_EMBED_MODEL", default="text-embedding-3-small")
        return _env_first("LLM_EMBED_MODEL", "OLLAMA_EMBED_MODEL", default="")

    @property
    def chat_model(self) -> str:
        if self.provider == "openai":
            return _env_first("LLM_CHAT_MODEL", default="gpt-4o-mini")
        return _env_first("LLM_CHAT_MODEL", "OLLAMA_CHAT_MODEL", default="")

    @property
    def cache_key(self) -> str:
        return f"{self.provider}|{self.base_url}|{self.embed_model}"

    def is_configured(self) -> bool:
        if not llm_setting_truthy("OLLAMA_ENABLED", "false"):
            return False
        if self.provider == "openai":
            return bool(self.base_url and self.api_key and self.embed_model and self.chat_model)
        return bool(self.base_url and self.embed_model and self.chat_model)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, url: str, *, timeout: int, **kwargs) -> requests.Response:
        try:
            return self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.ConnectionError:
            time.sleep(0.5)
            return self.session.request(method, url, timeout=timeout, **kwargs)

    def list_models(self) -> List[str]:
        if not self.base_url:
            return []
        try:
            r = self._request("GET", f"{self.base_url}/models", headers=self._headers(), timeout=10)
            if r.status_code != 200:
                return []
            data = r.json() or {}
            models = data.get("data")
            if not isinstance(models, list):
                return []
            return [m.get("id", "") for m in models if isinstance(m, dict) and m.get("id")]
        except Exception as e:
            logger.warning(f"OpenAI-compatible list_models failed: {e}", exc_info=True)
            return []

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self.is_configured() or not texts:
            return None
        try:
            payload = {"model": self.embed_model, "input": texts}
            r = self._request(
                "POST",
                f"{self.base_url}/embeddings",
                json=payload,
                headers=self._headers(),
                timeout=60,
            )
            if r.status_code != 200:
                logger.warning(f"OpenAI-compatible /embeddings returned {r.status_code}")
                return None
            rows = (r.json() or {}).get("data")
            if not isinstance(rows, list) or len(rows) != len(texts):
                logger.warning("OpenAI-compatible /embeddings returned unexpected payload shape")
                return None
            rows = sorted(rows, key=lambda row: row.get("index", 0) if isinstance(row, dict) else 0)
            vectors = [row.get("embedding") for row in rows if isinstance(row, dict)]
            if len(vectors) == len(texts) and all(isinstance(vec, list) for vec in vectors):
                return vectors
            logger.warning("OpenAI-compatible /embeddings returned missing embeddings")
            return None
        except Exception as e:
            logger.warning(f"OpenAI-compatible /embeddings failed: {e}", exc_info=True)
            return None

    def embed_one(self, text: str) -> Optional[List[float]]:
        vectors = self.embed([text])
        if vectors and len(vectors) == 1:
            return vectors[0]
        return None

    def _chat_payload(self, prompt: str, schema: Optional[dict], use_json_object: bool) -> dict:
        payload = {
            "model": self.chat_model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": 0,
            "max_tokens": _JUDGE_MAX_TOKENS,
        }
        if schema and not self._schema_format_unsupported:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "bookbridge_judge",
                    "schema": schema,
                    "strict": False,
                },
            }
        elif use_json_object and not self._json_object_unsupported:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post_chat(self, prompt: str, schema: Optional[dict], use_json_object: bool) -> requests.Response:
        return self._request(
            "POST",
            f"{self.base_url}/chat/completions",
            json=self._chat_payload(prompt, schema, use_json_object),
            headers=self._headers(),
            timeout=120,
        )

    def judge(self, prompt: str, schema: Optional[dict] = None) -> Optional[dict]:
        if not self.is_configured() or not prompt:
            return None
        try:
            r = self._post_chat(prompt, schema, True)
            if r.status_code == 400 and schema and not self._schema_format_unsupported:
                self._schema_format_unsupported = True
                logger.info("OpenAI-compatible server rejected JSON schema; falling back to JSON object mode")
                r = self._post_chat(prompt, None, True)
            if r.status_code == 400 and not self._json_object_unsupported:
                self._json_object_unsupported = True
                logger.info("OpenAI-compatible server rejected JSON object mode; falling back to prompt-only JSON")
                r = self._post_chat(prompt, None, False)
            if r.status_code != 200:
                logger.warning(f"OpenAI-compatible /chat/completions returned {r.status_code}")
                return None
            choices = (r.json() or {}).get("choices") or []
            if not choices:
                return None
            content = ((choices[0].get("message") or {}).get("content") or "").strip()
            if not content:
                return None
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
            logger.warning("OpenAI-compatible judge returned non-object JSON")
            return None
        except Exception as e:
            logger.warning(f"OpenAI-compatible judge failed: {e}", exc_info=True)
            return None
