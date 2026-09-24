#!/bin/bash
# Запуск AI Browser сервера
cd "$(dirname "$0")"

# Убить старый сервер
pkill -f "uvicorn web_backend" 2>/dev/null
sleep 1

# Запустить
# 127.0.0.1 принципиально: интерфейс умеет управлять браузером,
# 0.0.0.0 отдал бы этот контроль всей локальной сети.
../ai_browser_venv/bin/python -m uvicorn web_backend:app --port 8765 --host 127.0.0.1
