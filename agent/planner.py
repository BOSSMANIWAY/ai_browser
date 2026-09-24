"""
Agent Planner v2 - LLM-планировщик с tool calling

Отличия от v1:
- Вход: BrowserState (element_id) вместо сырого DOM
- Выход: {plan, thought, action} вместо плоского JSON
- Нормализация: любые синонимы tool'ов маппятся на канонические
- Устойчивый парсинг: markdown-обёртки, текст вокруг JSON, обрезанный JSON
"""

import json
import logging
import re
from typing import Dict, Any, Optional, List

from prompts.system import AGENT_SYSTEM_PROMPT, AGENT_USER_PROMPT_TEMPLATE
from agent.user_profile import profile_to_prompt

logger = logging.getLogger(__name__)

# Regex для извлечения чистого URL из Markdown-ссылок [text](url)
_MARKDOWN_URL_RE = re.compile(r"\[.*?\]\((https?://[^)]+)\)")


def _sanitize_url(raw: str) -> str:
    """Извлечь чистый URL из Markdown-ссылки [text](url) или вернуть как есть."""
    raw = str(raw).strip()
    m = _MARKDOWN_URL_RE.search(raw)
    if m:
        cleaned = m.group(1)
        logger.debug(f"Sanitized URL from markdown: {raw!r} -> {cleaned!r}")
        return cleaned
    return raw

# Канонические tool'ы
VALID_TOOLS = {
    "navigate", "click", "click_at", "hover", "drag", "type", "key", "scroll", "select",
    "new_tab", "switch_tab", "close_tab", "extract", "wait", "wait_user",
    "evaluate", "finish", "fill_form",
}

# Синонимы → канонические имена
TOOL_SYNONYMS = {
    "navigate_to": "navigate", "navigate_to_url": "navigate", "go_to": "navigate",
    "go_to_url": "navigate", "open_url": "navigate", "open": "navigate", "goto": "navigate",
    "fill": "type", "input": "type", "write": "type",
    "submit_form": "click", "submit": "click",
    "press": "key", "keypress": "key", "keyboard": "key",
    "complete": "finish", "done": "finish", "task_complete": "finish",
    "answer": "finish", "stop": "finish",
    "js": "evaluate", "javascript": "evaluate", "run_js": "evaluate",
    "screenshot": "wait",  # скриншот как отдельное действие не нужен — делаем всегда
    "confirm_action": "key", "request_confirmation": "key",
    "fetch_url": "navigate", "search": "navigate",
}


class AgentPlanner:
    """Планировщик v2: BrowserState → LLM → {plan, thought, action}"""

    def __init__(self, llm_client: Any, model: str = ""):
        self.llm_client = llm_client
        self.model = model

    async def decide(
        self,
        task: str,
        browser_state_text: str,
        tabs_text: str,
        last_action_result: str,
        recent_history: List[Dict[str, Any]],
        step: int,
        rejected_warning: str = "",
        user_messages: str = "",
        screenshot_b64: str = "",
        sheet_order: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Принятие решения. Возвращает нормализованное действие.
        screenshot_b64: PNG-base64 (sheet уровня или viewport) — если задан,
        передаётся модели как изображение (vision). Нужен для визуальных
        уровней игры, где текстовое состояние не показывает картинки.
        sheet_order — порядок eID клеток на sheet (строки сверху вниз)."""
        history_text = "\n".join(
            f"- {h.get('action', '?')}: {str(h.get('result', ''))[:150]}"
            for h in recent_history[-5:]
        ) or "(нет истории)"

        user_prompt = AGENT_USER_PROMPT_TEMPLATE.format(
            task=task,
            user_profile=profile_to_prompt() + rejected_warning + user_messages,
            last_action_result=last_action_result or "(первый шаг)",
            browser_state=browser_state_text,
            tabs=tabs_text,
            recent_history=history_text,
            step=step,
        )
        if screenshot_b64:
            if sheet_order:
                rows: List[List[str]] = []
                per = 4
                for i in range(0, len(sheet_order), per):
                    rows.append(sheet_order[i:i + per])
                order_text = "\n".join(
                    f"ряд {i+1}: {', '.join(r)}" for i, r in enumerate(rows)
                )
                user_prompt += (
                    "\n\n=== КАРТИНКА УРОВНЯ (vision) ===\n"
                    "Приложено изображение: клетки игровой сетки, вырезанные из "
                    "фотографии и собранные в ряды. Под каждой клеткой белая "
                    "подпись — её element id (eN). Порядок рядов:\n"
                    f"{order_text}\n"
                    "Опиши, что видишь в каждой клетке (объекты, знаки, цвета), "
                    "и реши задачу уровня. НЕ придумывай содержимое: если картинка "
                    "не отображается у тебя — честно скажи 'не вижу изображение'."
                )
            else:
                user_prompt += (
                    "\n\n=== СКРИНШОТ СТРАНИЧКИ (vision) ===\n"
                    "Приложен PNG-скриншот viewport. На нём видно то, что видит "
                    "человек: картинки в клетках сетки, знаки, фигуры, цвета. "
                    "Текстовое состояние выше может НЕ отражать содержимое "
                    "картинок — верь скриншоту. Координаты на скриншоте "
                    "совпадают с координатами center=(x,y) в списке элементов."
                )
        full_prompt = AGENT_SYSTEM_PROMPT + "\n\n" + user_prompt

        try:
            # LLM-вызов ограничен по времени: зависший curl/SSE не должен
            # блокировать шаг агента навсегда. Доставка картинки в pplx
            # НЕСТАБИЛЬНА (флап ~20-70%), поэтому при ответе "не вижу" —
            # до 8 попыток с паузами, смена формулировок не нужна: та же
            # картинка доходит со временем.
            import asyncio as _aio
            response = ""
            vision_ok = False
            vision_attempts = 8 if screenshot_b64 else 1
            for vision_attempt in range(vision_attempts):
                if screenshot_b64:
                    response = await _aio.wait_for(
                        self.llm_client.chat_with_image(full_prompt, screenshot_b64),
                        timeout=120,
                    )
                    resp_low = (response or "").lower()
                    lost_markers = (
                        "не вижу", "не могу напрямую", "не могу увидеть",
                        "нет доступа", "прикрепи", "пришлите изображение",
                        "пришлите картинку", "не был загружен", "не загружено",
                        "issue with the model", "не могу посмотреть",
                        "не могу определить", "не увидел", "нет изображения",
                        "не видно", "не отображается", "не отображаются",
                        "не могу описать", "не виден", "не видна",
                        "наугад", "наудачу", "угадыва", "не могу разглядеть",
                        "не могу рассмотреть", "картинка не", "фото не",
                    )
                    if response and not any(m in resp_low for m in lost_markers):
                        vision_ok = True
                        break
                    logger.warning(f"Vision lost (attempt {vision_attempt + 1}/{vision_attempts}): "
                                   f"{(response or '')[:120]}")
                    import asyncio as _s
                    await _s.sleep(3 * (vision_attempt + 1) if vision_attempt > 2 else 2)
                else:
                    response = await _aio.wait_for(self.llm_client.chat(full_prompt), timeout=90)
                    vision_ok = True
                    break
            if not vision_ok:
                # Все попытки слепые: ЗАПРЕЩАЕМ действия по угадыванию.
                # Следующий шаг заново соберёт sheet и повторит vision.
                logger.warning("Vision lost after all attempts — действие заблокировано")
                return {"tool": "wait", "seconds": 5,
                        "thought": "Изображение не доставлено — жду и повторю захват"}
            logger.info(f"LLM raw (first 300): {response[:300]}")

            action = self._parse_response(response)
            if action:
                logger.info(f"Decision: {action.get('tool')} — {action.get('thought', '')[:100]}")
                return action

            logger.warning("Failed to parse LLM response")
            return {"tool": "wait", "thought": "Не удалось распознать ответ LLM", "seconds": 2}

        except Exception as e:
            logger.error(f"LLM decision error: {e}", exc_info=True)
            return {"tool": "wait", "thought": f"Ошибка LLM: {e}", "seconds": 3}

    # ─── Парсинг ────────────────────────────────────────────────────

    def _parse_response(self, response: str) -> Optional[Dict[str, Any]]:
        """Устойчивый парсинг JSON из ответа LLM"""
        # 1. Пробуем прямой парсинг
        parsed = self._try_json(response.strip())
        if parsed is not None:
            return self._normalize(parsed)

        # 2. Ищем JSON в markdown-блоках ```json ... ```
        for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL):
            parsed = self._try_json(m.group(1))
            if parsed is not None:
                return self._normalize(parsed)

        # 3. Ищем первый { ... последний }
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end > start:
            parsed = self._try_json(response[start:end + 1])
            if parsed is not None:
                return self._normalize(parsed)

        # 4. Обрезанный JSON (LLM не закрыл скобки) — дополняем
        start = response.find("{")
        if start != -1:
            fragment = response[start:]
            # Пробуем дополнить закрывающими скобками
            for suffix in ("}", '"}}', '"}', "}}"):
                parsed = self._try_json(fragment + suffix)
                if parsed is not None:
                    return self._normalize(parsed)

        return None

    def _try_json(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    def _normalize(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Нормализация: синонимы tool'ов, вложенные структуры, параметры"""

        # action может быть вложен или на верхнем уровне
        action = data.get("action")
        if not isinstance(action, dict):
            # Плоский формат: {"type": "click", ...} или {"tool": "click", ...}
            action = dict(data)
            # Убираем служебные поля
            for k in ("plan", "thought", "reasoning"):
                action.pop(k, None)

        # Имя tool'а: tool / type / action / name / next_action
        tool = (action.get("tool") or action.get("type") or action.get("action") or
                action.get("name") or data.get("next_action") or action.get("next_action") or "")
        tool = str(tool).lower().strip()

        # Синонимы
        if tool in TOOL_SYNONYMS:
            tool = TOOL_SYNONYMS[tool]

        # submit_form/fill_form с полями → fill_form (композитное действие)
        has_fields = any(
            isinstance(action.get(k), dict) and action[k]
            for k in ("fields", "form_data", "inputs")
        )
        if has_fields and tool in ("click", "type"):
            tool = "fill_form"

        # search → navigate с google
        if tool == "search":
            query = action.get("query") or action.get("text") or ""
            tool = "navigate"
            action["url"] = f"https://www.google.com/search?q={query}"

        if tool not in VALID_TOOLS:
            logger.warning(f"Unknown tool: {tool}")
            return None

        # fill_form: композитное действие — заполняет несколько полей и опционально отправляет
        if tool == "fill_form":
            fields = action.get("fields") or action.get("form_data") or action.get("inputs") or {}
            if isinstance(fields, dict) and fields:
                normalized: Dict[str, Any] = {
                    "tool": "fill_form",
                    "fields": {str(k): str(v) for k, v in fields.items()},
                    "form_id": str(action.get("form_id") or action.get("form") or ""),
                    "submit": bool(action.get("submit", True)),  # по умолчанию отправляем
                }
                normalized["thought"] = str(data.get("thought") or action.get("thought") or "")
                plan = data.get("plan")
                if isinstance(plan, list):
                    normalized["plan"] = [str(p) for p in plan[:10]]
                return normalized
            # Пустые поля → обычный type
            tool = "type"

        # Нормализация параметров
        normalized: Dict[str, Any] = {"tool": tool}

        if tool == "navigate":
            url = action.get("url") or action.get("target") or ""
            if isinstance(url, list) and url:
                url = url[0]
            normalized["url"] = _sanitize_url(url)

        elif tool == "click":
            # element_id / eid / selector / target
            eid = action.get("element_id") or action.get("eid") or action.get("target") or action.get("selector") or ""
            eid = str(eid).lstrip("#")
            normalized["element_id"] = eid
            if not (eid.startswith("e") and eid[1:].isdigit()):
                normalized["text_fallback"] = True

        elif tool in ("click_at", "hover"):
            normalized["x"] = float(action.get("x", 0))
            normalized["y"] = float(action.get("y", 0))

        elif tool == "drag":
            normalized["x1"] = float(action.get("x1", action.get("from_x", 0)))
            normalized["y1"] = float(action.get("y1", action.get("from_y", 0)))
            normalized["x2"] = float(action.get("x2", action.get("to_x", 0)))
            normalized["y2"] = float(action.get("y2", action.get("to_y", 0)))
            normalized["steps"] = int(action.get("steps", 12))

        elif tool == "type":
            eid = str(action.get("element_id") or action.get("eid") or action.get("selector") or "").lstrip("#")
            normalized["element_id"] = eid
            normalized["text"] = str(action.get("text") or action.get("value") or "")
            # JSON иногда содержит строковые boolean-значения; bool("false") == True.
            def _as_bool(value: Any, default: bool) -> bool:
                if value is None:
                    return default
                if isinstance(value, bool):
                    return value
                if isinstance(value, (int, float)):
                    return bool(value)
                return str(value).strip().lower() not in ("false", "0", "no", "нет", "")
            normalized["clear"] = _as_bool(action.get("clear"), True)
            normalized["submit"] = _as_bool(action.get("submit"), False)

        elif tool == "key":
            normalized["key"] = str(action.get("key") or "Enter")

        elif tool == "scroll":
            normalized["direction"] = str(action.get("direction") or "down")
            normalized["amount"] = int(action.get("amount") or 600)

        elif tool == "select":
            normalized["element_id"] = str(action.get("element_id") or action.get("eid") or "").lstrip("#")
            normalized["value"] = str(action.get("value") or action.get("text") or "")

        elif tool in ("new_tab",):
            normalized["url"] = _sanitize_url(action.get("url") or "")

        elif tool in ("switch_tab", "close_tab"):
            normalized["tab_id"] = str(action.get("tab_id") or action.get("target") or "")

        elif tool == "extract":
            normalized["selector"] = str(action.get("selector") or "body")

        elif tool == "wait":
            try:
                normalized["seconds"] = min(float(action.get("seconds") or 2), 10)
            except (ValueError, TypeError):
                normalized["seconds"] = 2

        elif tool == "wait_user":
            normalized["reason"] = str(action.get("reason") or action.get("text") or "нужны данные от пользователя")
            try:
                normalized["timeout"] = min(float(action.get("timeout") or 300), 900)
            except (ValueError, TypeError):
                normalized["timeout"] = 300

        elif tool == "evaluate":
            normalized["expression"] = str(action.get("expression") or action.get("code") or "")

        elif tool == "finish":
            reason = (action.get("reason") or action.get("text") or
                      action.get("answer") or data.get("thought") or "Задача выполнена")
            normalized["reason"] = str(reason)

        # Мысль и план — на верхний уровень
        normalized["thought"] = str(data.get("thought") or action.get("thought") or "")
        plan = data.get("plan")
        if isinstance(plan, list):
            normalized["plan"] = [str(p) for p in plan[:10]]

        # Пометка об отвергнутых данных (вердикт LLM, рантайм запоминает)
        if action.get("rejected_value"):
            normalized["rejected_value"] = str(action["rejected_value"])
            if action.get("rejected_message"):
                normalized["rejected_message"] = str(action["rejected_message"])

        return normalized