"""
Web Backend - FastAPI сервер для веб-интерфейса AI Browser
"""

import asyncio
import json
import logging
import os
import platform
import subprocess
import uuid
from typing import AsyncGenerator
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from browser.controller import BrowserController
from browser.tab_manager import TabManager
from browser.screenshot import ScreenshotCapture
from browser.agent_overlay import AgentOverlay
from agent.loop import AgentLoop
from agent.memory import AgentMemory
from agent.llm_client import LLMClient
from agent.memory import AgentMemory
from agent.llm_client import LLMClient
from analysis.page_analyzer import PageAnalyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ai_browser_web")

# Флаг: вкладку веб-интерфейса уже открыли (main.py или auto_launch_chrome) —
# защита от дублирующихся вкладок 127.0.0.1:8765 при старте.
_webui_opened = False


def claim_webui_opening() -> bool:
    """Захватить право открыть вкладку веб-UI. True = открыть можешь ты."""
    global _webui_opened
    if _webui_opened:
        return False
    _webui_opened = True
    return True


app = FastAPI(title="AI Browser Agent")


def find_chrome_path():
    """Найти путь к Chrome на системе"""
    paths = {
        "darwin": [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ],
        "linux": [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium-browser",
        ],
        "win32": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
    }
    system = platform.system().lower()
    for p in paths.get(system, []):
        if os.path.exists(p):
            return p
    return None


async def auto_launch_chrome():
    """Запустить Chrome с CDP если ещё не запущен"""
    import socket
    
    # Проверяем, запущен ли Chrome с CDP на порту 9222
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex(('127.0.0.1', 9222))
        sock.close()
        if result == 0:
            logger.info("Chrome already running with CDP on port 9222")
            return
    except Exception:
        pass
    
    # Chrome не запущен — запускаем
    chrome_path = find_chrome_path()
    if not chrome_path:
        logger.warning("Chrome not found — agent will launch managed Chrome via Playwright")
        return

    # macOS Chrome — одиночный процесс: если Chrome уже запущен без CDP,
    # повторный запуск с флагом просто откроет окно в существующем процессе,
    # а порт 9222 не поднимется. Поэтому сначала корректно закрываем Chrome.
    try:
        probe = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
        chrome_running = probe.returncode == 0
    except Exception:
        chrome_running = False

    if chrome_running:
        logger.info("Chrome already running without CDP — restarting it with CDP")
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "Google Chrome" to quit'],
                timeout=10,
            )
            for _ in range(10):
                probe = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True)
                if probe.returncode != 0:
                    break
                await asyncio.sleep(1)
        except Exception as e:
            logger.warning(f"Failed to quit existing Chrome: {e}")

    logger.info(f"Launching Chrome with CDP: {chrome_path}")
    try:
        # Chrome 136+ игнорирует --remote-debugging-port при профиле по
        # умолчанию — CDP поднимается только с отдельным user-data-dir.
        # Директория та же, что у fallback в controller.connect(): куки
        # сохраняются между сессиями, конфликтов блокировок нет (пути
        # взаимоисключающие).
        profile_dir = Path(__file__).parent / "ai_browser_profile"
        profile_dir.mkdir(exist_ok=True)
        subprocess.Popen([
            chrome_path,
            f"--user-data-dir={profile_dir}",
            "--remote-debugging-port=9222",
            "--no-first-run",
            "--no-default-browser-check",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("Chrome launched — waiting for CDP to be ready...")
        # Ждём пока Chrome поднимет CDP
        for _ in range(30):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex(('127.0.0.1', 9222))
                sock.close()
                if result == 0:
                    logger.info("Chrome CDP ready")
                    # Перезапуск Chrome закрыл вкладку с веб-интерфейсом —
                    # открываем заново, только если её ещё никто не открыл
                    # (main.py открывает свою через 1с после старта).
                    if claim_webui_opening():
                        import webbrowser
                        webbrowser.open("http://127.0.0.1:8765")
                    return
            except Exception:
                pass
            await asyncio.sleep(1)
        logger.warning("Chrome launched but CDP not ready in time")
    except Exception as e:
        logger.error(f"Failed to launch Chrome: {e}")


@app.on_event("startup")
async def startup_event():
    """Автозапуск Chrome при старте сервера"""
    asyncio.create_task(auto_launch_chrome())

# Глобальное состояние
browser_controller: BrowserController = None
agent_loop: AgentLoop = None
memory: AgentMemory = None
llm_client: LLMClient = None
tab_manager: TabManager = None
page_analyzer: PageAnalyzer = None
agent_overlay: AgentOverlay = None
is_connected = False
current_task_id = None


def get_or_create_controller():
    """Получить или создать контроллер браузера"""
    global browser_controller, agent_loop, memory, llm_client, tab_manager, page_analyzer, agent_overlay

    if browser_controller is None:
        browser_controller = BrowserController()
        memory = AgentMemory()
        llm_client = LLMClient(provider="pplx", model="claude47opus")
        tab_manager = TabManager(browser_controller)
        page_analyzer = PageAnalyzer(llm_client, "claude47opus")
        agent_overlay = AgentOverlay(browser_controller)

    return browser_controller


@app.get("/")
async def serve_index():
    return FileResponse(Path("web") / "index.html")


@app.get("/api/network_report")
async def network_report(reload: bool = True, bodies: bool = False):
    """Отчёт по сетевой активности активной вкладки (аналог вкладки Сеть в DevTools)."""
    from browser.network_monitor import capture_network, format_network_report
    try:
        data = await capture_network(reload=reload, include_bodies=bodies)
        return {"report": format_network_report(data), "total": data["total"]}
    except Exception as e:
        logger.error(f"network_report error: {e}", exc_info=True)
        return {"error": str(e)}


@app.get("/web/{path:path}")
async def serve_static(path: str):
    """Отдать статические файлы"""
    file_path = Path("web") / path
    if file_path.exists():
        return FileResponse(file_path)
    return FileResponse("web/index.html")


@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket для общения с чатом"""
    await websocket.accept()
    logger.info(f"Client connected: {client_id}")

    try:
        while True:
            # Получаем сообщение от клиента
            data = await websocket.receive_text()
            message = json.loads(data)
            action = message.get("action", "")
            payload = message.get("payload", "")

            if action == "connect":
                # Подключение к Chrome
                await websocket.send_json({
                    "type": "status",
                    "message": " Подключение к Chrome..."
                })

                try:
                    global browser_controller, agent_loop, memory, llm_client, tab_manager, page_analyzer
                    controller = get_or_create_controller()
                    await controller.connect()

                    agent_loop = AgentLoop(
                        controller=controller,
                        memory=memory,
                        llm_client=llm_client,
                        max_steps=50,
                        model="claude47opus",
                    )

                    browser_controller = controller
                    is_connected = True

                    tabs = await controller.get_tabs()
                    # Конвертируем TabInfo в словари для JSON
                    tabs_data = [{"tab_id": t.tab_id, "url": t.url, "title": t.title, "is_active": t.is_active} for t in tabs]
                    await websocket.send_json({
                        "type": "connected",
                        "tabs": tabs_data,
                        "message": " Подключено к Chrome"
                    })

                except Exception as e:
                    logger.error(f"Connection error: {e}")
                    await websocket.send_json({
                        "type": "error",
                        "message": f"[ERROR] Ошибка подключения: {e}\n\nУбедитесь, что Chrome запущен с --remote-debugging-port=9222"
                    })

            elif action == "task":
                # Запуск задачи агента
                prompt = payload.get("prompt", "")
                provider = payload.get("provider", "pplx")
                model = payload.get("model", "claude47opus")
                if not prompt:
                    await websocket.send_json({"type": "error", "message": "Нет задачи"})
                    continue

                task_id = str(uuid.uuid4())[:8]

                # Запускаем агента в фоне
                asyncio.create_task(run_agent_task(websocket, task_id, prompt, provider, model))

            elif action == "user_input":
                # Пользователь передал данные агенту (код из SMS, пароль и т.п.)
                value = payload.get("value", "") if isinstance(payload, dict) else str(payload)
                if agent_loop is not None:
                    await agent_loop.provide_user_input(value)
                    await websocket.send_json({"type": "status", "message": " Данные переданы агенту"})
                else:
                    await websocket.send_json({"type": "error", "message": "Агент не запущен"})

            elif action == "stop_task":
                # Остановка задачи агента
                if agent_loop is not None:
                    agent_loop.stop()
                    await websocket.send_json({"type": "status", "message": " Остановка задачи..."})
                else:
                    await websocket.send_json({"type": "error", "message": "Агент не запущен"})

            elif action == "tabs":
                # Список вкладок
                tabs = await browser_controller.get_tabs()
                tabs_data = [{"tab_id": t.tab_id, "url": t.url, "title": t.title, "is_active": t.is_active} for t in tabs]
                await websocket.send_json({"type": "tabs", "tabs": tabs_data})

            elif action == "navigate":
                # Навигация
                url = payload.get("url", "")
                await browser_controller.navigate(url)
                await websocket.send_json({"type": "status", "message": f" Переход: {url}"})

            elif action == "screenshot":
                # Скриншот
                sc = ScreenshotCapture(browser_controller)
                data = await sc.capture_viewport()
                await websocket.send_json({"type": "screenshot", "data": data})

            elif action == "summarize":
                # Суммаризация
                dom_data = await browser_controller._page.evaluate("""
                    () => ({
                        title: document.title,
                        url: window.location.href,
                        text: document.body.innerText.substring(0, 5000)
                    })
                """)
                summary = await page_analyzer.summarize_page(dom_data)
                await websocket.send_json({"type": "summary", "text": summary})

            elif action == "group_tabs":
                # Группировка
                groups = await tab_manager.auto_group_tabs()
                # Конвертируем в словари
                groups_data = {name: [{"tab_id": t.tab_id, "url": t.url, "title": t.title, "is_active": t.is_active} for t in tabs] for name, tabs in groups.items()}
                await websocket.send_json({"type": "groups", "groups": groups_data})

            elif action == "disconnect":
                # Отключение
                await browser_controller.disconnect()
                is_connected = False
                await websocket.send_json({"type": "disconnected", "message": "Отключено"})

    except WebSocketDisconnect:
        logger.info(f"Client disconnected: {client_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}", exc_info=True)


async def run_agent_task(websocket: WebSocket, task_id: str, prompt: str, provider: str = "pplx", model: str = "claude47opus"):
    """Запуск задачи агента. Весь цикл — в AgentLoop v2, здесь только WebSocket-мост."""
    global agent_loop

    controller = browser_controller
    mem = AgentMemory()
    llm = LLMClient(provider=provider, model=model)

    async def send_progress(text: str):
        try:
            await websocket.send_json({"type": "progress", "task_id": task_id, "text": text})
        except Exception:
            pass  # клиент мог отключиться

    async def send_finish(reason: str):
        try:
            await websocket.send_json({
                "type": "task_complete", "task_id": task_id, "reason": reason
            })
            await websocket.send_json({"type": "task_end", "task_id": task_id})
        except Exception:
            pass

    task_loop = AgentLoop(
        controller=controller,
        memory=mem,
        llm_client=llm,
        max_steps=40,
        model=model,
        on_progress=send_progress,
        on_finish=send_finish,
    )
    agent_loop = task_loop

    await websocket.send_json({
        "type": "task_start",
        "task_id": task_id,
        "prompt": prompt
    })

    try:
        await task_loop.run(prompt)
    except Exception as e:
        logger.error(f"Task error: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error", "task_id": task_id, "message": f"Ошибка: {e}"
            })
        except Exception:
            pass
    finally:
        try:
            await websocket.send_json({"type": "task_end", "task_id": task_id})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
