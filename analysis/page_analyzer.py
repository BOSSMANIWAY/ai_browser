"""
Page Analyzer - Анализ и суммаризация страниц
Анализ PDF, статей, документов
"""

import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class PageAnalyzer:
    """Анализатор содержимого страниц"""

    def __init__(self, llm_client: Any, model: str = "claude47opus"):
        self.llm_client = llm_client
        self.model = model

    async def summarize_page(self, dom_data: Dict[str, Any]) -> str:
        """Суммаризация страницы"""
        text = dom_data.get("text", "")
        title = dom_data.get("title", "Без заголовка")
        url = dom_data.get("url", "N/A")

        if not text or len(text) < 100:
            return f"Страница '{title}' содержит мало текста для анализа."

        # Отправляем текст LLM для суммаризации
        prompt = f"""Проанализируй и кратко суммируй содержимое страницы.

Заголовок: {title}
URL: {url}

Текст страницы (первые 4000 символов):
{text[:4000]}

Верни краткую суммаризацию (3-5 предложений) на русском языке.
Укажи:
1. О чём страница
2. Ключевые факты/данные
3. Есть ли формы для заполнения
4. Есть ли ссылки на другие страницы
"""

        try:
            summary = await self.llm_client.chat(prompt)
            return summary
        except Exception as e:
            logger.error(f"Summarization error: {e}")
            return f"Ошибка суммаризации: {e}"

    async def analyze_pdf(self, pdf_url: str) -> str:
        """Анализ PDF-документа (через встраивание в iframe)"""
        prompt = f"""Проанализируй PDF-документ по адресу: {pdf_url}

Я вижу iframe с PDF. Опиши:
1. Что содержится в документе
2. Ключевые данные
3. Структура документа

Если PDF не отображается, скажи об этом.
"""

        try:
            result = await self.llm_client.chat(prompt)
            return result
        except Exception as e:
            logger.error(f"PDF analysis error: {e}")
            return f"Ошибка анализа PDF: {e}"

    async def extract_form_data(self, dom_data: Dict[str, Any]) -> Dict[str, Any]:
        """Извлечение данных из форм"""
        forms = dom_data.get("forms", [])
        inputs = dom_data.get("inputs", [])

        extracted = {
            "forms_count": len(forms),
            "inputs_count": len(inputs),
            "form_fields": [],
        }

        for inp in inputs:
            field = {
                "name": inp.get("name", ""),
                "id": inp.get("id", ""),
                "type": inp.get("type", "text"),
                "placeholder": inp.get("placeholder", ""),
                "required": inp.get("required", False),
            }
            extracted["form_fields"].append(field)

        return extracted

    async def detect_login_page(self, dom_data: Dict[str, Any]) -> bool:
        """Определение страницы входа"""
        alerts = dom_data.get("alerts", [])
        text = dom_data.get("text", "").lower()

        login_indicators = [
            "sign in", "log in", "войти", "авторизация",
            "login", "password", "пароль", "email", "почта"
        ]

        for indicator in login_indicators:
            if indicator in text:
                return True

        if "login_detected" in alerts:
            return True

        return False

    async def detect_captcha(self, dom_data: Dict[str, Any]) -> bool:
        """Обнаружение CAPTCHA"""
        alerts = dom_data.get("alerts", [])
        text = dom_data.get("text", "").lower()

        if "captcha_detected" in alerts:
            return True

        captcha_keywords = ["captcha", "recaptcha", "verify", "подтверждение"]
        for kw in captcha_keywords:
            if kw in text:
                return True

        return False

    async def analyze_page_structure(self, dom_data: Dict[str, Any]) -> Dict[str, Any]:
        """Анализ структуры страницы"""
        metrics = {
            "title": dom_data.get("title", ""),
            "url": dom_data.get("url", ""),
            "links_count": len(dom_data.get("links", [])),
            "buttons_count": len(dom_data.get("buttons", [])),
            "forms_count": len(dom_data.get("forms", [])),
            "inputs_count": len(dom_data.get("inputs", [])),
            "images_count": len(dom_data.get("images", [])),
            "iframes_count": len(dom_data.get("iframes", [])),
            "has_login": await self.detect_login_page(dom_data),
            "has_captcha": await self.detect_captcha(dom_data),
            "text_length": len(dom_data.get("text", "")),
        }
        return metrics
