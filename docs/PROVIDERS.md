# Подключение LLM-провайдеров

Все модели в ai_browser подключаются через **один реестр** — `agent/providers.py`.
Ни цикл агента, ни планировщик, ни веб-интерфейс не знают имён вендоров: они
спрашивают у реестра список, а `LLMClient` выбирает транспорт по полю `kind`.

Поэтому подключить нового вендора — это **одна запись в словаре**, а не правка
в нескольких файлах.

---

## Содержание

- [Как это работает](#как-это-работает)
- [Быстрый старт](#быстрый-старт)
- [Встроенные провайдеры](#встроенные-провайдеры)
- [Типы транспорта](#типы-транспорта)
- [Смена эндпоинта (Kimi .cn ↔ .ai, свой прокси)](#смена-эндпоинта)
- [Vision (скриншоты)](#vision-скриншоты)
- [Добавить своего вендора](#добавить-своего-вендора)
- [Что видит веб-интерфейс](#что-видит-веб-интерфейс)
- [Безопасность ключей](#безопасность-ключей)
- [Частые проблемы](#частые-проблемы)

---

## Как это работает

```
main.py / web_backend.py / planner / page_analyzer
        │
        └── LLMClient(provider="deepseek")          agent/llm_client.py
                    │
                    ├── читает ProviderSpec         agent/providers.py
                    └── выбирает транспорт по kind:
                          openai  → POST {base_url}/chat/completions
                          ollama  → POST {base_url}/api/chat
                          pplx    → curl + SSE, cookies-сессия
```

Контракт, который не должен ломаться при доработках:

```python
client = LLMClient(provider="kimi", model="")   # model="" → дефолт из реестра
await client.chat("текст запроса") -> str
await client.chat_with_image("текст", image_b64, filename, mime) -> str
client.provider  # str
client.model     # str
```

Единственное место, где модель подставляется в запрос, — `LLMClient`.
`PageAnalyzer` и `Planner` модель не выбирают.

---

## Быстрый старт

1. Скопируйте шаблон и заполните **только свой** провайдер:

   ```bash
   cp .env.example .env
   # отредактируйте .env
   set -a; source .env; set +a
   ```

2. Посмотрите, что доступно и где какого ключа не хватает:

   ```bash
   python3 main.py --list-providers
   ```

3. Запустите:

   ```bash
   python3 main.py --provider deepseek --task "открой example.com"
   # или веб-режим: ./start.sh → http://127.0.0.1:8765
   ```

---

## Встроенные провайдеры

| Ключ | Вендор | kind | Переменная ключа | Дефолт модели | Vision |
|---|---|---|---|---|---|
| `pplx` | Perplexity AI *(по умолчанию)* | pplx | cookies-файл | `claude47opus` | да |
| `openai` | OpenAI | openai | `OPENAI_API_KEY` | `gpt-4o-mini` | да |
| `kimi` | Moonshot Kimi | openai | `KIMI_API_KEY` | `kimi-k2-0905-preview` | да |
| `ollama` | Ollama (локально) | ollama | — | `qwen3:32b` | да |
| `deepseek` | DeepSeek | openai | `DEEPSEEK_API_KEY` | `deepseek-chat` | нет |
| `groq` | Groq | openai | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | нет |
| `mistral` | Mistral | openai | `MISTRAL_API_KEY` | `mistral-large-latest` | да |
| `openrouter` | OpenRouter | openai | `OPENROUTER_API_KEY` | `anthropic/claude-sonnet-4.5` | да |
| `gemini` | Google Gemini (compat) | openai | `GEMINI_API_KEY` | `gemini-2.5-flash` | да |
| `xai` | xAI Grok | openai | `XAI_API_KEY` | `grok-4` | да |
| `lmstudio` | LM Studio (локально) | openai | `LMSTUDIO_API_KEY` *(опц.)* | `local-model` | да |
| `localai` | LocalAI (локально) | openai | `LOCALAI_API_KEY` *(опц.)* | `qwen3:32b` | да |

Ключи `pplx`, `kimi`, `ollama`, `openai` исторически зашиты в коде и тестах —
переименовывать их нельзя.

> Актуальный список с эндпоинтами всегда даёт `--list-providers`: реестр и есть
> источник правды, таблица выше — только справочник.

---

## Типы транспорта

### `openai` — OpenAI-совместимый API

Самый распространённый: под него попадают OpenAI, Kimi, DeepSeek, Groq,
Mistral, OpenRouter, Gemini (compat-слой), xAI, LM Studio, LocalAI и десятки
других. Требуются три вещи: `base_url`, имя переменной с ключом и название
модели.

```bash
export GROQ_API_KEY="gsk_..."
python3 main.py --provider groq --task "..."
```

Авторизация — `Authorization: Bearer <ключ>`. Если ключ не задан и он
обязателен, клиент **не падает**, а возвращает `{"type":"wait", ...}` — агент
делает паузу и пробует снова.

### `ollama` — локальный Ollama

Ключ не нужен, сервер локальный:

```bash
ollama serve                 # если не запущен
ollama pull qwen3:32b        # текстовая модель
ollama pull qwen3-vl:8b      # vision-модель (для скриншотов)
python3 main.py --provider ollama --model qwen3-vl:8b --task "..."
```

### `pplx` — Perplexity через cookies (бесплатный ИИ без API-ключа)

Официального платного API здесь нет, поэтому используется авторизованная
сессия браузера: запрос уходит на тот же эндпоинт, что и сайт, —
`POST https://www.perplexity.ai/rest/sse/perplexity_ask` (curl + SSE).
Ваша бесплатная аккаунт-сессия Perplexity = бесплатный ИИ для агента.

**Что именно нужно собрать.** Транспорт передаёт cookie одной строкой в
`curl -b`, то есть требуется **значение HTTP-заголовка `Cookie:`**
(`name=value; name2=value2`), а НЕ файл в формате Netscape. Экспорт
расширениями «Get cookies.txt» напрямую не подойдёт — они пишут
tab-разделённый формат.

Сбор через DevTools (надёжный способ):

1. Войдите на https://www.perplexity.ai в Chrome и отправьте любой вопрос.
2. Откройте DevTools (`F12`) → вкладка **Network**.
3. Найдите запрос `perplexity_ask` (фильтр по `sse` или по имени).
4. В заголовках запроса скопируйте **полное** значение заголовка `cookie:`
   одной строкой. Ключевые cookie: `__Secure-1PSID`, `__Secure-1PSIDTS`,
   `pplx-ss` — без `__Secure-*` (они httpOnly) запрос не пройдёт.
5. Вставьте в файл `.pplx_cookies.txt` в корне проекта (в одну строку)
   либо укажите путь: `export PPLX_COOKIES_FILE=/path/to/file`.
6. Необязательно: `PPLX_ACCOUNT_ID` — id аккаунта из cookie `user`
   (заголовок `x-pplx-account`). Без него запросы тоже проходят.

Тот же результат даёт правый клик по запросу `perplexity_ask` →
**Copy → Copy as cURL**: из него можно вырезать и строку `-b '...'`.

Файл cookies перечислен в `.gitignore`. Это **доступ к вашему аккаунту**:
не коммитьте его и не передавайте никому. Срок жизни cookies ограничен —
при истечении сессии начнут приходить ошибки `HTTP 403`, файл нужно обновить
(повторить шаги 1–5).

---

## Смена эндпоинта

Эндпоинт любого провайдера переопределяется переменной `<КЛЮЧ>_BASE_URL` —
без правки кода:

```bash
# Kimi: по умолчанию включён китайский эндпоинт api.moonshot.cn.
# Ключ, выданный на platform.moonshot.ai, с ним НЕ работает:
export KIMI_BASE_URL=https://api.moonshot.ai/v1

# OpenAI через корпоративный прокси / самохостed-шлюз:
export OPENAI_BASE_URL=https://llm-proxy.internal.example/v1

# Ollama на другой машине:
export OLLAMA_BASE_URL=http://192.168.1.20:11434
```

Значение применяется при каждом обращении к `chat_url`, то есть достаточно
экспортировать переменную перед запуском. Для `pplx` переменная не действует:
там адрес зашит в SSE-транспорте.

---

## Vision (скриншоты)

Агент отправляет скриншот страницы, когда работает с игровыми/визуальными
уровнями и когда планировщику нужен разбор интерфейса. Транспорт картинки
тоже зависит от `kind`:

| kind | как уходит картинка |
|---|---|
| `openai` | `content: [{type:"text"...}, {type:"image_url", image_url:{url:"data:image/png;base64,..."}}]` |
| `ollama` | `messages[0].images: ["<чистый base64>"]` |
| `pplx` | `params.attachments: [{file_view:{kind:"image", mime_type, base64}, filename}]` |

Флаг `vision` в реестре — обещание вендора принимать картинки. Провайдер с
`vision: False` не отваливается с ошибкой: `chat_with_image()` для него
деградирует до текстового `chat()`, и агент работает вслепую по DOM. UI честно
пишет об этом «только текст».

Практический смысл: для работы с картинками выбирайте `openai`, `kimi`,
`ollama` + vision-модель, `pplx`. `deepseek` и `groq` в текущем реестре —
текстовые.

---

## Добавить своего вендора

### Вариант A — запись в реестр (постоянно)

`agent/providers.py`, словарь `PROVIDERS`:

```python
PROVIDERS["myvendor"] = ProviderSpec(
    key="myvendor",
    label="My Vendor",
    kind="openai",                                  # совместимый API
    base_url="https://api.myvendor.com/v1",
    api_key_env="MYVENDOR_API_KEY",
    default_model="mv-large",
    models=[_m("mv-large", "MV Large", best=True),
            _m("mv-mini", "MV Mini")],
    vision=True,
    docs_url="https://myvendor.com/settings/api",
    note="",
)
```

Готово: `--provider myvendor`, пункт в веб-селекторе и ответ `/api/providers`
появятся сами. `import os` в модуле уже есть, `register_provider` вызывать не
обязательно.

### Вариант B — регистрация в рантайме (без правки репозитория)

Полезно в форках и при интеграциях:

```python
from agent.providers import ProviderSpec, ModelInfo, register_provider

register_provider(ProviderSpec(
    key="internal", label="Внутренний LLM", kind="openai",
    base_url="https://llm.corp.internal/v1",
    api_key_env="INTERNAL_LLM_KEY", default_model="corp-1",
    models=[ModelInfo("corp-1", "Corp 1", best=True)],
    vision=True, requires_key=True,
))
```

Регистрируйте **до** создания `LLMClient` — клиент читает реестр в конструкторе.

### Чего делать не нужно

- Не добавляйте веток `if provider == "..."` в `llm_client.py` — разветвление
  идёт по `kind`, а не по имени.
- Не дублируйте список моделей в `web/index.html` — селекторы заполняются из API.
- Не меняйте ключи `pplx`/`kimi`/`ollama`/`openai`.

Если новый вендор не совместим с OpenAI (свой формат запроса, как у Ollama или
Perplexity) — добавьте новый `kind` и метод `_chat_<kind>` в `LLMClient`,
иначе запись в реестр не поможет.

---

## Что видит веб-интерфейс

`GET /api/providers` отдаёт реестр в виде JSON без секретов — фронтенд строит
по нему оба селектора и подсказку под ними:

```json
{
  "key": "kimi",
  "label": "Moonshot Kimi",
  "kind": "openai",
  "default_model": "kimi-k2-0905-preview",
  "vision": true,
  "requires_key": true,
  "key_env": "KIMI_API_KEY",
  "has_key": false,
  "base_url_env": "KIMI_BASE_URL",
  "base_url": "https://api.moonshot.cn/v1",
  "docs_url": "https://platform.moonshot.ai/console/api-keys",
  "note": "",
  "models": [{"id": "kimi-k2-0905-preview", "name": "Kimi K2", "best": true}]
}
```

- `has_key` — **только** признак наличия ключа в окружении сервера, сам ключ
  не передаётся никогда. Для `pplx` равен `null` (проверить дёшево нельзя).
- `base_url` — фактически используемый адрес с учётом `*_BASE_URL`: сразу видно,
  что Kimi смотрит в `.cn`, хотя ключ международный.

WebSocket-задача принимает `{provider, model}`; неизвестный провайдер
отклоняется до запуска задачи, `model: ""` резолвится в дефолт реестра.

---

## Безопасность ключей

- Ключи живут **только** в окружении или в `.env`. `.env`, `.env.*` и
  `.pplx_cookies.txt` в `.gitignore`; отслеживается лишь `.env.example`.
- `.env.example` — шаблон без значений, его коммитить можно и нужно.
- Публичный `/api/providers` секреты не отдаёт, но и не должен становиться
  точкой доступа: веб-фронтент слушает `127.0.0.1`, наружу его не пробрасывайте.
- Ключ, попавший в историю Git, считается скомпрометированным: отзывайте его у
  вендора, одного `git rm` недостаточно.

---

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| `KIMI_API_KEY not set` | Ключ не экспортирован в то окружение, где запущен процесс. `source .env` в том же шелле. |
| Kimi отвечает `401 invalid api key` при верном ключе | Ключ международный, а эндпоинт китайский: `export KIMI_BASE_URL=https://api.moonshot.ai/v1` |
| `model not found` | Название модели есть только у вендора, в реестре его нет. Добавьте в `models` или передайте `--model`. |
| Perplexity: `HTTP 403` / пустые ответы | Cookies истекли или включился антибот — обновите `.pplx_cookies.txt`, снизьте частоту запросов. |
| Ollama: `Connection refused` | Сервер не запущен (`ollama serve`) или указан не тот `OLLAMA_BASE_URL`. |
| Агент «не видит» скриншоты | У провайдера `vision: False` — выберите vision-модель. |
| Веб-UI показывает пустой список | Бэкенд не перезапущен после правки реестра; `/api/providers` читается на загрузке страницы. |
| `httpx not installed` | Зависимости не установлены в активное окружение: `pip install -r requirements.txt`. |
