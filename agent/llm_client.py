"""
LLM Client Adapter - Универсальный адаптер для LLM
Прямые HTTP-вызовы к Perplexity API (через cookies), Kimi API, Ollama API, OpenAI API
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
import uuid as _uuid
from typing import Optional, List

logger = logging.getLogger(__name__)

# ─── Perplexity credentials ────────────────────────────────────────────────────────────────
# Пир-доступ к Perplexity работает через cookies авторизованной сессии.
# Ничего из этого не хранится в репозитории: значения приходят из
# переменных окружения или из локального файла .pplx_cookies.txt
# (перечислен в .gitignore). Без них провайдер pplx просто не будет
# выбран — остальные (kimi/ollama/openai) работают без cookies.

PPLX_ACCOUNT_ID = os.environ.get("PPLX_ACCOUNT_ID", "")

# Cookie-файл искорем в самом проекте, а не по относительному пути наружу:
# так клон репозитория остаётся самодостаточным.
_COOKIE_FILE = os.environ.get(
    "PPLX_COOKIES_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".pplx_cookies.txt"),
)
try:
    with open(_COOKIE_FILE, "r") as _f:
        PPLX_COOKIES = _f.read().strip()
except OSError:
    PPLX_COOKIES = ""

# Идентификаторы запроса/сессии — одноразовые, генерируются на каждый запуск.
# Захардкоженные значения позволили бы ассоциировать запросы с конкретным
# аккаунтом, а заодно утекли бы в публичный репозиторий.
REQUEST_UUID = os.environ.get("PPLX_REQUEST_UUID") or str(_uuid.uuid4())
RUM_SESSION_ID = os.environ.get("PPLX_RUM_SESSION_ID") or str(_uuid.uuid4())
PPLX_CONTEXT_UUID = os.environ.get("PPLX_CONTEXT_UUID") or str(_uuid.uuid4())

# Список самых умных моделей для каждого провайдера
MODELS = {
    "pplx": [
        {"id": "claude47opus", "name": "Claude 4.7 Opus (Pplx)", "best": True},
        {"id": "claude46sonnet", "name": "Claude 4.6 Sonnet (Pplx)"},
        {"id": "gpt55", "name": "GPT-5.5 (Pplx)"},
        {"id": "gpt54", "name": "GPT-5.4 (Pplx)"},
        {"id": "grok4", "name": "Grok 4 (Pplx)"},
        {"id": "claudecode", "name": "Claude Code (Pplx)"},
        {"id": "codex4", "name": "Codex 4 (Pplx)"},
        {"id": "gemini30flash", "name": "Gemini 3.0 Flash (Pplx)"},
        {"id": "o3pro", "name": "o3 Pro (Pplx)"},
        {"id": "turbo", "name": "Turbo (Pplx)"},
    ],
    "kimi": [
        {"id": "k2d5", "name": "K2 D5 (Kimi)", "best": True},
        {"id": "k2", "name": "K2 (Kimi)"},
        {"id": "moonshot-v1-8k", "name": "Moonshot v1 8K"},
        {"id": "moonshot-v1-32k", "name": "Moonshot v1 32K"},
        {"id": "moonshot-v1-128k", "name": "Moonshot v1 128K"},
    ],
    "ollama": [
        {"id": "qwen3:235b-a22b", "name": "Qwen3 235B (Ollama)", "best": True},
        {"id": "qwen3:32b", "name": "Qwen3 32B (Ollama)"},
        {"id": "qwen3:8b", "name": "Qwen3 8B (Ollama)"},
        {"id": "claude46sonnet", "name": "Claude 4.6 Sonnet (Ollama)"},
        {"id": "gpt55", "name": "GPT-5.5 (Ollama)"},
        {"id": "deepseek-r1:70b", "name": "DeepSeek R1 70B (Ollama)"},
        {"id": "llama3.3:70b", "name": "Llama 3.3 70B (Ollama)"},
    ],
    "openai": [
        {"id": "gpt-5.1", "name": "GPT-5.1 (OpenAI)", "best": True},
        {"id": "gpt-5", "name": "GPT-5 (OpenAI)"},
        {"id": "gpt-4o", "name": "GPT-4o (OpenAI)"},
        {"id": "o3", "name": "o3 (OpenAI)"},
        {"id": "o4-mini", "name": "o4 Mini (OpenAI)"},
        {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4 (OpenAI)"},
        {"id": "claude-opus-4-20250514", "name": "Claude Opus 4 (OpenAI)"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro (OpenAI)"},
    ],
}


class LLMClient:
    """
    Адаптер для LLM через прямые API-вызовы.
    Поддерживает: Perplexity (через cookies), Kimi, Ollama, OpenAI (GPT)
    """

    def __init__(self, provider: str = "pplx", model: str = "claude47opus"):
        self.provider = provider
        self.model = model

    async def chat(self, message: str) -> str:
        """Отправить сообщение и получить ответ"""
        if self.provider == "pplx":
            return await self._chat_pplx(message)
        elif self.provider == "kimi":
            return await self._chat_kimi(message)
        elif self.provider == "ollama":
            return await self._chat_ollama(message)
        elif self.provider == "openai":
            return await self._chat_openai(message)
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

    async def chat_with_image(self, message: str, image_b64: str,
                              filename: str = "image.png",
                              mime: str = "image/png") -> str:
        """Сообщение + изображение (vision). pplx — через file_view-attachment.
        ВАЖНО: PNG единственный надёжно доставляемый формат (JPEG/WebP
        теряются бэкендом всегда). Остальные провайдеры пока без vision —
        падают в обычный chat."""
        if self.provider == "pplx":
            return await self._chat_pplx(message, image_b64=image_b64,
                                         image_filename=filename, image_mime=mime)
        return await self.chat(message)

    async def _chat_pplx(self, message: str, image_b64: str = "",
                         image_filename: str = "screenshot.jpg",
                         image_mime: str = "image/jpeg") -> str:
        """Вызов Perplexity AI через cookies и curl (как pplx_caller.py).
        image_b64 — чистый base64 (без data:-префикса); пустая строка = текстовый запрос."""
        try:
            model_pref = self.model or "turbo"

            attachments = []
            if image_b64:
                attachments = [{
                    "file_view": {
                        "kind": "image",
                        "mime_type": image_mime,
                        "base64": image_b64,
                    },
                    "filename": image_filename,
                }]

            payload = {
                "params": {
                    "attachments": attachments,
                    "language": "ru-RU",
                    "timezone": "Europe/Kaliningrad",
                    "search_focus": "internet",
                    "sources": ["web"],
                    "frontend_uuid": REQUEST_UUID,
                    "mode": "copilot",
                    "model_preference": model_pref,
                    "is_related_query": False,
                    "is_sponsored": False,
                    "frontend_context_uuid": PPLX_CONTEXT_UUID,
                    "prompt_source": "user",
                    "query_source": "home",
                    "is_incognito": False,
                    "local_search_enabled": True,
                    "use_schematized_api": True,
                    "send_back_text_in_streaming_api": False,
                    "supported_block_use_cases": [
                        "answer_modes", "media_items", "knowledge_cards",
                        "inline_entity_cards", "place_widgets", "finance_widgets",
                        "sports_widgets", "news_widgets", "shopping_widgets",
                        "jobs_widgets", "search_result_widgets", "inline_images",
                        "inline_assets", "placeholder_cards", "diff_blocks",
                        "inline_knowledge_cards", "entity_group_v2",
                        "refinement_filters", "canvas_mode", "maps_preview",
                        "answer_tabs", "preserve_latex", "in_context_suggestions",
                        "pending_followups", "inline_claims", "unified_assets",
                        "workflow_steps", "workflow_widgets", "navigation_results",
                        "background_agents",
                    ],
                    "client_coordinates": None,
                    "mentions": [],
                    "dsl_query": message,
                    "skip_search_enabled": True,
                    "is_nav_suggestions_disabled": False,
                    "source": "entropy",
                    "always_search_override": False,
                    "override_no_search": False,
                    "comet_info": {"rendering_place": "tab"},
                    "client_search_results_cache_key": f"nav-{REQUEST_UUID}",
                    "should_ask_for_mcp_tool_confirmation": True,
                    "supports_tool_approval_modal": True,
                    "browser_agent_allow_once_from_toggle": False,
                    "force_enable_browser_agent": False,
                    "supported_features": ["browser_agent_permission_banner_v1.1"],
                    "extended_context": False,
                    "local_workspace_directories": [],
                    "version": "2.18",
                    "rum_session_id": RUM_SESSION_ID,
                },
                "query_str": message,
            }

            with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as f:
                json.dump(payload, f)
                payload_file = f.name

            headers = [
                "-H", "accept: text/event-stream",
                "-H", "accept-language: ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "-H", "cache-control: no-cache",
                "-H", "content-type: application/json",
                "-H", "origin: https://www.perplexity.ai",
                "-H", "pragma: no-cache",
                "-H", "priority: u=1, i",
                "-H", "referer: https://www.perplexity.ai/?erp=new_tab",
                "-H", 'sec-ch-ua: "Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
                "-H", 'sec-ch-ua-arch: "arm"',
                "-H", 'sec-ch-ua-bitness: "64"',
                "-H", 'sec-ch-ua-full-version: "151.0.7922.47"',
                '-H', 'sec-ch-ua-full-version-list: "Not=A?Brand";v="99.0.0.0", "Google Chrome";v="151.0.7922.47", "Chromium";v="151.0.7922.47"',
                "-H", "sec-ch-ua-mobile: ?0",
                '-H', 'sec-ch-ua-model: ""',
                "-H", 'sec-ch-ua-platform: "macOS"',
                '-H', 'sec-ch-ua-platform-version: "26.2.0"',
                "-H", "sec-fetch-dest: empty",
                "-H", "sec-fetch-mode: cors",
                "-H", "sec-fetch-site: same-origin",
                "-H", "user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
                "-H", f"x-pplx-account: {PPLX_ACCOUNT_ID}",
                "-H", "x-perplexity-request-endpoint: https://www.perplexity.ai/rest/sse/perplexity_ask",
                "-H", "x-perplexity-request-reason: ask-query-state-provider",
                "-H", "x-perplexity-request-try-number: 1",
                "-H", f"x-request-id: {REQUEST_UUID}",
                "-b", PPLX_COOKIES,
            ]

            cmd = [
                "curl", "--http2", "-s", "-N", "-w", "\nHTTP_CODE:%{http_code}",
                "-X", "POST",
                "https://www.perplexity.ai/rest/sse/perplexity_ask",
                "--data-binary", f"@{payload_file}",
            ] + headers

            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            raw = proc.stdout.read()
            proc.wait()

            text = raw.decode("utf-8", errors="ignore")

            # Cloudflare/рейт-лимит: 4xx отдаёт HTML-заглушку вместо SSE.
            # Раньше это «проглатывалось» и выглядело как пустой ответ LLM —
            # честно сигнализируем ошибку, чтобы сработал retry-механизм.
            http_line = [l for l in text.splitlines() if l.startswith("HTTP_CODE:")]
            if not text.strip().startswith("event:") and (http_line or "<html" in text.lower()[:200]):
                code = http_line[0].split(":", 1)[1].strip() if http_line else "?"
                logger.error(f"Perplexity HTTP {code} (rate limit / CF block)")
                return json.dumps(
                    {"type": "wait", "thought": f"Pplx error: HTTP {code}", "seconds": 15},
                    ensure_ascii=False,
                )

            # SSE: собираем data-блоки (JSON может быть разбит на несколько строк)
            data_blocks = []
            current_block = ""
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped.startswith("data:"):
                    current_block += stripped[5:]
                elif stripped == "event:" or stripped == "":
                    if current_block.strip():
                        data_blocks.append(current_block.strip())
                        current_block = ""

            full_text = ""
            for json_str in data_blocks:
                try:
                    event = json.loads(json_str)
                except json.JSONDecodeError:
                    continue

                is_final = event.get("final_sse_message") or event.get("final")
                status = event.get("status", "")

                for block in event.get("blocks", []):
                    if "workflow_block" in block:
                        wb = block["workflow_block"]
                        if is_final and status == "COMPLETED":
                            for step in wb.get("steps", []):
                                for item in step.get("items", []):
                                    if item.get("type") == "WORKFLOW_ITEM_TEXT":
                                        tp = item.get("payload", {}).get("text_payload", {})
                                        full_text = tp.get("text", "")
                                        if full_text:
                                            os.unlink(payload_file)
                                            return full_text
                        elif not is_final:
                            for step in wb.get("steps", []):
                                for item in step.get("items", []):
                                    if item.get("type") == "WORKFLOW_ITEM_TEXT":
                                        tp = item.get("payload", {}).get("text_payload", {})
                                        chunks = tp.get("chunks", [])
                                        if chunks:
                                            text_val = "".join(chunks)
                                            if text_val and len(text_val) > 5:
                                                full_text = text_val
                    # Ответ также приходит в markdown_block (answer при DONE,
                    # chunks при стриминге) — у запросов с attachments ответ
                    # стабильно идёт этим путём.
                    if "markdown_block" in block:
                        mb = block["markdown_block"]
                        ans = mb.get("answer", "")
                        if ans:
                            full_text = ans
                        else:
                            chunks = mb.get("chunks", [])
                            if chunks:
                                text_val = "".join(chunks)
                                if text_val and len(text_val) > 5:
                                    full_text = text_val

            os.unlink(payload_file)
            return full_text if full_text else '{"type": "wait", "thought": "Perplexity вернул пустой ответ", "seconds": 1}'

        except Exception as e:
            logger.error(f"Pplx error: {e}")
            return json.dumps({"type": "wait", "thought": f"Pplx error: {e}", "seconds": 1}, ensure_ascii=False)

    async def _chat_kimi(self, message: str) -> str:
        """Вызов Kimi AI через API"""
        try:
            import httpx
            api_key = os.environ.get("KIMI_API_KEY", "")
            if not api_key:
                logger.warning("KIMI_API_KEY not set. Using fallback.")
                return '{"type": "wait", "thought": "Kimi API key не настроен", "seconds": 1}'

            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    "https://api.moonshot.cn/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model or "moonshot-v1-8k",
                        "messages": [{"role": "user", "content": message}],
                        "max_tokens": 2048,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content

        except ImportError:
            logger.error("httpx not installed.")
            return '{"type": "wait", "thought": "httpx не установлен", "seconds": 1}'
        except Exception as e:
            logger.error(f"Kimi error: {e}")
            return json.dumps({"type": "wait", "thought": f"Kimi error: {e}", "seconds": 1}, ensure_ascii=False)

    async def _chat_ollama(self, message: str) -> str:
        """Вызов Ollama через локальный API"""
        try:
            import httpx
            model = self.model or "qwen3:235b-a22b"

            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    "http://localhost:11434/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": message}],
                        "stream": False,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data.get("message", {}).get("content", "")
                return content

        except ImportError:
            logger.error("httpx not installed.")
            return '{"type": "wait", "thought": "httpx не установлен", "seconds": 1}'
        except Exception as e:
            logger.error(f"Ollama error: {e}")
            return json.dumps({"type": "wait", "thought": f"Ollama error: {e}", "seconds": 1}, ensure_ascii=False)

    async def _chat_openai(self, message: str) -> str:
        """Вызов OpenAI (GPT) через REST API"""
        try:
            import httpx
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                logger.warning("OPENAI_API_KEY not set. Using fallback.")
                return '{"type": "wait", "thought": "OpenAI API key не настроен", "seconds": 1}'

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model or "gpt-5.1",
                        "messages": [{"role": "user", "content": message}],
                        "max_tokens": 2048,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content

        except ImportError:
            logger.error("httpx not installed.")
            return '{"type": "wait", "thought": "httpx не установлен", "seconds": 1}'
        except Exception as e:
            logger.error(f"OpenAI error: {e}")
            return json.dumps({"type": "wait", "thought": f"OpenAI error: {e}", "seconds": 1}, ensure_ascii=False)