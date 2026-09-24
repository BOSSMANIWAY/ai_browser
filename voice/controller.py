"""
Voice Controller - Голосовое управление браузером
Распознавание речи → команды агента
"""

import logging
import asyncio
from typing import Optional, Callable, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class VoiceController:
    """
    Голосовое управление через SpeechRecognition (Google STT).
    Поддержка команд на русском и английском.
    """

    def __init__(self, agent_loop=None):
        self.agent_loop = agent_loop
        self.listening = False
        self._callback: Optional[Callable] = None
        self._recognition = None
        self._speech_recognizer = None

    async def initialize(self):
        """Инициализация модуля распознавания речи"""
        try:
            import speech_recognition as sr
            self._speech_recognizer = sr.Recognizer()
            self._speech_recognizer.energy_threshold = 300
            self._speech_recognizer.dynamic_energy_threshold = True
            logger.info("Voice controller initialized")
            return True
        except ImportError:
            logger.warning("speech_recognition not installed. Run: pip install SpeechRecognition PyAudio")
            return False

    async def start_listening(self, callback: Callable):
        """
        Запуск прослушивания микрофона.
        callback(text) будет вызван при распознавании команды.
        """
        self._callback = callback
        self.listening = True

        if not self._speech_recognizer:
            await self.initialize()

        import speech_recognition as sr

        microphone = sr.Microphone()

        logger.info("🎤 Слушаю... Говорите команду.")
        print("\n🎤 Голосовое управление активировано. Говорите...")

        while self.listening:
            try:
                with microphone as source:
                    # Калибровка на тишину
                    self._speech_recognizer.adjust_for_ambient_noise(source)

                    # Запись аудио
                    audio = self._speech_recognizer.listen(source, timeout=5, phrase_time_limit=10)

                # Распознавание (Google Free API)
                try:
                    text = self._speech_recognizer.recognize_google(audio, language="ru-RU")
                    logger.info(f"Распознано: {text}")
                    print(f"🗣️ Вы сказали: {text}")

                    if self._callback:
                        await self._callback(text)

                except sr.UnknownValueError:
                    logger.debug("Речь не распознана")
                except sr.RequestError as e:
                    logger.error(f"Ошибка API распознавания: {e}")

            except Exception as e:
                logger.error(f"Ошибка прослушивания: {e}")
                await asyncio.sleep(1)

    async def stop_listening(self):
        """Остановка прослушивания"""
        self.listening = False
        logger.info("Голосовое управление остановлено")
        print("🎤 Голосовое управление остановлено")

    def parse_voice_command(self, text: str) -> Dict[str, Any]:
        """
        Парсинг голосовой команды в действие агента.
        """
        text_lower = text.lower().strip()

        # Команды навигации
        if any(kw in text_lower for kw in ["открой", "перейди", "зайди на", "открой сайт"]):
            url = self._extract_url(text)
            if url:
                return {"type": "navigate", "url": url, "thought": f"Голосовая команда: открыть {url}"}

        # Команды клика
        if any(kw in text_lower for kw in ["нажми", "кликни", "нажми на"]):
            text_match = self._extract_action_target(text, ["нажми", "кликни"])
            if text_match:
                return {"type": "click", "text": text_match, "thought": f"Голосовая команда: нажать '{text_match}'"}

        # Команды ввода
        if any(kw in text_lower for kw in ["введи", "напиши", "впиши"]):
            text_match = self._extract_action_target(text, ["введи", "напиши"])
            if text_match:
                return {"type": "type", "text": text_match, "thought": f"Голосовая команда: ввести '{text_match}'"}

        # Команды прокрутки
        if "прокрути" in text_lower or "листай" in text_lower:
            if "вниз" in text_lower:
                return {"type": "scroll", "direction": "down", "amount": 500, "thought": "Голосовая команда: прокрутить вниз"}
            elif "вверх" in text_lower:
                return {"type": "scroll", "direction": "up", "amount": 500, "thought": "Голосовая команда: прокрутить вверх"}

        # Команда новой вкладки
        if any(kw in text_lower for kw in ["новая вкладка", "открой новую"]):
            url = self._extract_url(text)
            return {"type": "new_tab", "url": url or None, "thought": "Голосовая команда: новая вкладка"}

        # Команда скриншота
        if any(kw in text_lower for kw in ["скриншот", "сделай фото", "сфотографируй"]):
            return {"type": "screenshot", "thought": "Голосовая команда: сделать скриншот"}

        # Команда суммаризации
        if any(kw in text_lower for kw in ["суммируй", "что на странице", "прочитай"]):
            return {"type": "extract", "selector": "body", "thought": "Голосовая команда: извлечь текст страницы"}

        # Команда завершения
        if any(kw in text_lower for kw in ["хватит", "стоп", "заверши", "довольно"]):
            return {"type": "finish", "reason": f"Голосовая команда: {text}", "thought": "Голосовая команда: завершить"}

        # По умолчанию — интерпретируем как задачу
        return {"type": "evaluate", "expression": f"console.log('{text}')", "thought": f"Голосовая команда (интерпретация): {text}"}

    def _extract_url(self, text: str) -> Optional[str]:
        """Извлечение URL из текста команды"""
        import re
        # Ищем URL
        url_match = re.search(r'(https?://[^\s]+)', text)
        if url_match:
            return url_match.group(1)

        # Ищем домен
        domain_match = re.search(r'(?:открой|перейди|зайди на|сайт)\s+(\S+)', text, re.IGNORECASE)
        if domain_match:
            domain = domain_match.group(1).strip('.,!?')
            if not domain.startswith('http'):
                domain = 'https://' + domain
            return domain

        return None

    def _extract_action_target(self, text: str, keywords: list) -> Optional[str]:
        """Извлечение цели действия из команды"""
        import re
        for kw in keywords:
            match = re.search(rf'{kw}\s+(.+?)(?:\.|$)', text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None