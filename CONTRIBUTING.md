# Вклад в проект

Спасибо, что хотите улучшить AI Browser Agent! Ниже — как всё устроено,
чтобы ваш PR прошёл ревью быстро.

## Формат

- Ветку назовите `feat/<что>` или `fix/<что>`.
- Коммит — по конвенции Conventional Commits:
  `feat: …`, `fix: …`, `docs: …`, `refactor: …`, `test: …`, `chore: …`.
  Пишите в теле **зачем**, а не что именно изменено.
- Один PR = одна логическая change. Рабочее дерево к моменту PR чистое
  (`git status` пустой).

## Что можно добавить проще всего

### Новый LLM-вендор

Правится только [agent/providers.py](agent/providers.py) — одна запись
`ProviderSpec`. Код цикла агента, планировщик, веб-UI и CLI менять не нужно:
они читают реестр через `GET /api/providers`.

```python
PROVIDERS["myvendor"] = ProviderSpec(
    key="myvendor", label="My Vendor", kind="openai",
    base_url="https://api.myvendor.com/v1",
    api_key_env="MYVENDOR_API_KEY", default_model="mv-large",
    models=[_m(("mv-large", "MV Large", True), ("mv-mini", "MV Mini"))],
    vision=True, docs_url="https://myvendor.com/settings/api",
)
```

Транспорт выбирается по полю `kind`, а не по имени вендора — поэтому веток
`if provider == "…"` в `agent/llm_client.py` быть не должно и в вашем PR.

### Новое действие агента

1. Действие исполняется в `browser/controller.py`.
2. Диспетч действий — `agent/loop.py`.
3. Описание и формат ответа LLM — `prompts/system.py`.
4. Добавьте/обновите таблицу действий в README.

## Проверки перед PR

```bash
python -m compileall -q .                      # синтаксис
python main.py --list-providers                # реестр и CLI живы
python -c "import web_backend"                 # импорт приложения
PYTHONPATH=. python tests/test_dialog_logic.py # логика модалок (без браузера/сети)
```

CI (`.github/workflows/ci.yml`) прогоняет то же самое — если локально зелёное,
PR почти наверняка пройдёт.

## Тесты

Тесты в `tests/` — это скрипты с чек-листами/asserts, а не pytest-наборы:
часть из них требует запущенного Chrome или сетевых ключей. Новые проверки
пишите так же: либо чистые функции-ассерты (запускаются в CI), либо скрипт
с явной пометкой, какой инфраструктуры он требует.

## Стиль

- Комментарии — редко и по делу (*зачем*, а не *что*); язык — русский.
- Не переименовывайте ключи реестра `pplx`/`kimi`/`ollama`/`openai` — они
  зашиты в код, тесты и WebSocket-контракт.
- Секреты не коммитятся никогда: ключи живут в env/`.env` (в `.gitignore`),
  файлы cookie — тоже.

## Ревью

Опишите в PR: проблему, решение, как проверяли, что подумали и отвергли.
Скриншот/GIF для изменений UI приветствуются.
