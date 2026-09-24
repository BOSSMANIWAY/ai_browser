#!/bin/bash
# Запуск AI Browser Agent
# Playwright сам запустит Chrome
pkill -f "main.py"
pkill -f "uvicorn web_backend"
clear 
echo " Запуск AI Browser Agent..."

# Определяем корневую директорию проекта
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Активируем venv и запускаем
cd "$SCRIPT_DIR"
VENV_PATH="$PROJECT_DIR/ai_browser_venv"

if [ -f "$VENV_PATH/bin/activate" ]; then
    source "$VENV_PATH/bin/activate"
    # caffeinate держит Mac активным: без него macOS может усыпить дисплей
    # и завершить Chrome по Automatic Termination (TAL) во время долгой задачи
    caffeinate -dimsu python3 main.py
else
    echo "[ERROR] venv не найден: $VENV_PATH"
    echo "   Запустите: pip install fastapi uvicorn websockets playwright"
    echo "   playwright install chromium"
    caffeinate -dimsu python3 main.py
fi
