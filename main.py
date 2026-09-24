#!/usr/bin/env python3
"""
AI Browser Agent - ИИ-управление браузером
Запуск: python3 main.py
Откроет Chrome с веб-интерфейсом чата
"""

import asyncio
import argparse
import logging
import sys
import os
import signal
import time
import webbrowser
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ai_browser.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ai_browser")

# Незапротоколированные падения в asyncio-задачах (uvicorn, websockets,
# playwright callbacks) пишем в лог: без этого процесс молча умирает
# и причину не видно (наблюдали на neal.fun: process exit без трейсбека).
import threading, traceback as _tb

def _log_thread_exception(args):
    logger.error(f"Необработанное исключение в потоке {args.thread}: {args.exc_type.__name__}: {args.exc_value}\n"
                 + "".join(_tb.format_exception(args.exc_type, args.exc_value, args.traceback)))

threading.excepthook = _log_thread_exception

def _log_loop_exception(loop, context):
    exc = context.get("exception")
    msg = context.get("message", "asyncio error")
    if exc:
        logger.error(f"Необработанное исключение в event loop: {msg}: {exc!r}\n"
                     + "".join(_tb.format_exception(type(exc), exc, exc.__traceback__)))
    else:
        logger.error(f"Ошибка event loop: {msg} ({context})")

import asyncio as _asyncio_setup

# Хвостовой дамп: если процесс завершается аварийно, в лог попадает причина.
import atexit as _atexit

def _exit_log():
    logger.warning("Процесс ai_browser завершается")

_atexit.register(_exit_log)

WEB_PORT = 8765
WEB_URL = f"http://127.0.0.1:{WEB_PORT}"

# Глобальное состояние для graceful shutdown
_shutdown_requested = False
_server = None


def handle_sigint(signum, frame):
    """Ctrl+C: первое нажатие — мягкая остановка, второе — немедленный выход.
    ВАЖНО: без блокирующих вызовов (sleep) — они замораживают event loop
    и ломают graceful shutdown uvicorn."""
    global _shutdown_requested
    if not _shutdown_requested:
        _shutdown_requested = True
        print("\n\n⏹️  Остановка... Нажмите Ctrl+C ещё раз для принудительного выхода\n")
        # Останавливаем сервер из event loop, а не из обработчика сигнала
        if _server is not None:
            _server.should_exit = True
    else:
        print("\n\n🛑 Принудительная остановка...")
        os._exit(1)


def print_banner():
    banner = """
╔══════════════════════════════════════════════════╗
║                                                  ║
║          AI BROWSER AGENT v1.0                 ║
║         ИИ-браузер в стиле Comet                 ║
║                                                  ║
║  Запуск веб-интерфейса...                        ║
║                                                  ║
╚══════════════════════════════════════════════════╝
    """
    print(banner)


def handle_sigint(signum, frame):
    """Ctrl+C: первое нажатие — мягкая остановка, второе — немедленный выход.
    ВАЖНО: без блокирующих вызовов (sleep) — они замораживают event loop
    и ломают graceful shutdown uvicorn."""
    global _shutdown_requested
    if not _shutdown_requested:
        _shutdown_requested = True
        print("\n\n⏹️  Остановка... Нажмите Ctrl+C ещё раз для принудительного выхода\n")
        # Останавливаем сервер из event loop, а не из обработчика сигнала
        if _server is not None:
            _server.should_exit = True
    else:
        print("\n\n🛑 Принудительная остановка...")
        os._exit(1)


async def start_web_server():
    """Запуск FastAPI сервера"""
    from web_backend import app

    import uvicorn

    # Ловим все необработанные исключения задач (agent task, websocket, playwright)
    try:
        asyncio.get_running_loop().set_exception_handler(_log_loop_exception)
    except RuntimeError:
        asyncio.get_event_loop_policy().get_event_loop().set_exception_handler(_log_loop_exception)

    print(f"\n🌐 Веб-интерфейс: {WEB_URL}")
    print("[INFO] Откроется Chrome автоматически\n")

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=WEB_PORT,
        log_level="info",
        access_log=False,
    )
    global _server
    _server = uvicorn.Server(config)

    # Открываем браузер (одна вкладка: если Chrome перезапускался и
    # auto_launch_chrome уже открыл вкладку — не дублируем)
    await asyncio.sleep(1)
    from web_backend import claim_webui_opening
    if claim_webui_opening():
        webbrowser.open(WEB_URL)

    try:
        await _server.serve()
    except asyncio.CancelledError:
        logger.info("Server cancelled")
    except OSError as e:
        # EADDRINUSE (errno 48 на macOS): порт занят другим процессом.
        # uvicorn печатает свой ERROR, но не завершает процесс — делаем это
        # мы, с понятным сообщением (иначе serve() вернётся и упадёт ниже).
        if e.errno == 48:
            print(f"\n[ОШИБКА] Порт {WEB_PORT} занят другим процессом.")
            print("Найди его:  lsof -nP -iTCP:8765 -sTCP:LISTEN")
            print("Останови (kill <pid>) и запусти ./start.sh снова.\n")
            os._exit(1)
        raise
    finally:
        # shutdown() валиден только после старта serve(); при ошибке bind
        # сервер не инициализирован и вызов падает с AttributeError.
        if _server and getattr(_server, "started", False):
            await _server.shutdown()


def main():
    parser = argparse.ArgumentParser(
        description="AI Browser Agent — ИИ-управление браузером",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  # Запуск веб-интерфейса (по умолчанию)
  python3 main.py

  # CLI режим с задачей
  python3 main.py --task "Зайди на google.com"

  # Выбор модели
  python3 main.py --task "Открой example.com" --model qwen3:8b --provider ollama
        """,
    )

    parser.add_argument(
        "--task", "-t",
        type=str,
        help="Задача для агента (CLI режим)",
    )
    parser.add_argument(
        "--model", "-m",
        type=str,
        default="",
        help="Модель LLM (по умолчанию — рекомендованная модель провайдера из реестра)",
    )
    parser.add_argument(
        "--provider", "-p",
        type=str,
        default="pplx",
        metavar="NAME",
        help="Провайдер LLM из реестра agent/providers.py "
             "(список: --list-providers)",
    )
    parser.add_argument(
        "--list-providers",
        action="store_true",
        help="Показать доступных провайдеров, их модели и переменные окружения",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Максимальное количество шагов",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Быстрая демонстрация",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Режим отладки",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Справочник по подключённым LLM-вендорам (реестр agent/providers.py).
    if args.list_providers:
        from agent.providers import list_providers
        print("\nДоступные провайдеры LLM:\n")
        for p in list_providers():
            need = "ключ не нужен" if not p["requires_key"] else f"env: {p['key_env']}"
            print(f"  {p['key']:<11} {p['label']:<24} {need}")
            print(f"  {'':<11} модель по умолчанию: {p['default_model']}"
                  f"{'  |  vision: да' if p['vision'] else ''}")
            if p.get("note"):
                print(f"  {'':<11} {p['note']}")
            print()
        print("Подключить нового вендора: docs/PROVIDERS.md\n")
        return

    if args.task:
        # CLI режим
        from browser.controller import BrowserController
        from agent.loop import AgentLoop
        from agent.memory import AgentMemory
        from agent.llm_client import LLMClient
        from agent.providers import get_provider, list_providers

        spec = get_provider(args.provider)
        if spec is None:
            known = ", ".join(p["key"] for p in list_providers())
            print(f"\n[ERROR] Неизвестный провайдер '{args.provider}'. Доступно: {known}")
            print("Подробности: python3 main.py --list-providers")
            return
        # Модель по умолчанию берём из реестра — иначе при смене вендора
        # улетит запрос к несуществующей модели другого провайдера.
        if not args.model:
            args.model = spec.default_model_id()

        async def run_cli():
            controller = BrowserController()
            memory = AgentMemory()
            llm = LLMClient(provider=args.provider, model=args.model)
            loop_agent = AgentLoop(
                controller=controller,
                memory=memory,
                llm_client=llm,
                max_steps=args.max_steps,
                model=args.model,
            )

            try:
                print("\n Подключение к Chrome...")
                user_data_dir = os.path.expanduser("~/Library/Application Support/Google/Chrome")
                await controller.connect(user_data_dir=user_data_dir)
                print(" Подключено к Chrome")
                await loop_agent.run(args.task)
            except KeyboardInterrupt:
                print("\n⏹️ Прервано")
            except Exception as e:
                logger.error(f"Ошибка: {e}", exc_info=True)
                print(f"\n[ERROR] Ошибка: {e}")
            finally:
                await controller.disconnect()

        asyncio.run(run_cli())

    elif args.demo:
        from browser.controller import BrowserController
        from browser.screenshot import ScreenshotCapture

        async def run_demo():
            controller = BrowserController()
            try:
                print("\n Подключение к Chrome...")
                user_data_dir = os.path.expanduser("~/Library/Application Support/Google/Chrome")
                await controller.connect(user_data_dir=user_data_dir)
                print("\n🌐 Открытие demo-страницы...")
                await controller.navigate("https://example.com")
                sc = ScreenshotCapture(controller)
                data = await sc.capture_viewport()
                dom = await sc.capture_dom_snapshot()
                print(f" Скриншот сделан")
                print(f" Страница: {dom.get('title')}")
                print(f"📝 Ссылок: {len(dom.get('links', []))}")
            except Exception as e:
                logger.error(f"Ошибка: {e}", exc_info=True)
                print(f"\n[ERROR] Ошибка: {e}")
            finally:
                await controller.disconnect()

        asyncio.run(run_demo())

    else:
        # Веб-интерфейс (по умолчанию)
        print_banner()
        # Регистрируем обработчик Ctrl+C
        signal.signal(signal.SIGINT, handle_sigint)
        asyncio.run(start_web_server())


if __name__ == "__main__":
    main()
