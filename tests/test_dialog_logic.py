"""Функциональная проверка анти-пинг-понга модалок (без браузера).

Проверяет: детект blocking-окна, кнопки, guard на отмену, счётчик повторов,
резолв битых element_id и вывод для LLM.
"""
import sys

from agent.loop import AgentLoop
from browser.browser_state import BrowserState


def make_state(dialog, elements):
    return BrowserState({
        "url": "https://hh.ru/resume/edit",
        "title": "Резюме",
        "elements": elements,
        "dialog": dialog,
    })


BLOCKING_DIALOG = {
    "open": True, "count": 1,
    "title": "Уточните специальность",
    "text": "Уточните специальность Выбрано 1 из 5 Автомобильный бизнес",
    "buttons": [
        {"eid": "e60", "text": "Информационные технологии"},
        {"eid": "e71", "text": "Сбросить"},
        {"eid": "e72", "text": "Сохранить и продолжить"},
    ],
    "blocking": True, "has_proceed": True, "has_cancel": True,
    "has_input": True, "has_required_field": False,
}

ELEMENTS = [
    {"eid": "e22", "tag": "input", "type": "text", "text": "Профессия",
     "x": 300, "y": 200, "width": 300, "height": 40, "in_viewport": True},
    {"eid": "e60", "tag": "button", "text": "Информационные технологии",
     "x": 300, "y": 400, "width": 200, "height": 40, "in_dialog": True, "in_viewport": True},
    {"eid": "e71", "tag": "button", "text": "Сбросить",
     "x": 200, "y": 700, "width": 120, "height": 40, "in_dialog": True, "in_viewport": True},
    {"eid": "e72", "tag": "button", "text": "Сохранить и продолжить",
     "x": 500, "y": 700, "width": 220, "height": 40, "in_dialog": True, "in_viewport": True},
    {"eid": "e73", "tag": "button", "aria": "Закрыть", "text": "",
     "x": 700, "y": 120, "width": 30, "height": 30, "in_dialog": True, "in_viewport": True},
]

PROMO_DIALOG = {
    "open": True, "count": 1, "title": "Акция", "text": "Скидка 50% Только сегодня",
    "buttons": [{"eid": "e90", "text": "ОК"}, {"eid": "e91", "text": "Закрыть"}],
    "blocking": False, "has_proceed": True, "has_cancel": True,
    "has_input": False, "has_required_field": False,
}


def new_loop():
    loop = object.__new__(AgentLoop)
    loop._dialog_open_counts = {}
    loop._dialog_prev_sig = None
    loop._dialog_hint_given = set()
    return loop


fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


loop = new_loop()
state = make_state(BLOCKING_DIALOG, ELEMENTS)

# 1. Сигнатура блокирующего окна
sig = loop._dialog_sig(state)
check("blocking dialog detected", sig is not None, repr(sig))
check("promo dialog not blocking", loop._dialog_sig(make_state(PROMO_DIALOG, ELEMENTS)) is None)

# 2. Кнопки отмены распознаны, «Сохранить» — не отмена
cancels = {c["eid"] for c in loop._cancel_buttons_in_dialog(state)}
check("cancel buttons = Сбросить + Закрыть", cancels == {"e71", "e73"}, str(cancels))
check("proceed button not blocked", "e72" not in cancels)

# 3. Guard: клик по «Сбросить» запрещён, по «Сохранить» — разрешён
blocked = loop._dialog_guard({"tool": "click", "element_id": "e71"}, state)
check("click Сбросить blocked", blocked is not None and not blocked["ok"])
check("hint names proceed button",
      blocked and "e72" in blocked["error"] and "Сохранить" in blocked["error"],
      (blocked or {}).get("error", "")[:120])
check("click Сохранить allowed", loop._dialog_guard({"tool": "click", "element_id": "e72"}, state) is None)
check("click option allowed", loop._dialog_guard({"tool": "click", "element_id": "e60"}, state) is None)
check("click_at on Сбросить blocked",
      loop._dialog_guard({"tool": "click_at", "x": 205, "y": 702}, state) is not None)
check("click_at far away allowed",
      loop._dialog_guard({"tool": "click_at", "x": 500, "y": 700}, state) is None)
check("promo close allowed",
      loop._dialog_guard({"tool": "click", "element_id": "e91"},
                         make_state(PROMO_DIALOG, ELEMENTS)) is None)
check("type not guarded", loop._dialog_guard({"tool": "type", "element_id": "e22"}, state) is None)

# 4. Пинг-понг: 1-е открытие — тихо, повтор после закрытия — подсказка
loop = new_loop()
check("first open silent", loop._track_dialog(state) is None)
check("continuous open silent", loop._track_dialog(state) is None)
check("closed -> no sig", loop._track_dialog(make_state({"open": False}, ELEMENTS)) is None)
hint = loop._track_dialog(state)
check("reopen gives hint", hint is not None and "ПИНГ-ПОНГ" in (hint or ""), (hint or "")[:120])
check("hint points to Сохранить", hint is not None and "e72" in hint)
check("reopen counter grows", loop._dialog_open_counts[sig] == 2)

# 5. Резолв element_id
loop = new_loop()
eid, err = loop._resolve_eid(state, "e22")
check("valid eid passes", eid == "e22" and err is None)
eid, err = loop._resolve_eid(state, "e?")
check("placeholder 'e?' rejected as id", eid is None and err and "заглушка" in err, (err or "")[:90])
eid, err = loop._resolve_eid(state, "e99")
check("stale eid rejected as id", eid is None and err and "нет в текущем состоянии" in err, (err or "")[:90])
eid, err = loop._resolve_eid(state, "Сбросить", text_fallback=True)
check("text fallback resolves", eid == "e71" and err is None)
eid, err = loop._resolve_eid(state, "Несуществующая кнопка", text_fallback=True)
check("unknown text rejected", eid is None and err is not None)
eid, err = loop._resolve_eid(state, "")
check("empty eid rejected", eid is None and err is not None)

# 6. Вывод для LLM
text = state.to_llm_text()
check("llm text: blocking warning", "БЛОКИРУЮЩЕЕ ОКНО" in text)
check("llm text: dialog buttons listed", "Кнопки окна" in text and "[e72]" in text)
check("llm text: forbids close", "крестиком" in text)

print()
print("FAILED:", fails if fails else "нет — все проверки пройдены")
sys.exit(1 if fails else 0)
