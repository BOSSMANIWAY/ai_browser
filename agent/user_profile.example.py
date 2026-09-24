"""
User Profile - персональные данные для автозаполнения форм

Используется в промпте агента: когда задача требует ввести личные данные
(регистрация, заказ, контактная форма), LLM берёт их отсюда, а не выдумывает.

ЭТО ШАБЛОН. Скопируйте его в `agent/user_profile.py` и подставьте свои
значения:

    cp agent/user_profile.example.py agent/user_profile.py

`agent/user_profile.py` намеренно исключён из репозитория (.gitignore) —
реальные персональные данные не должны попадать в git.
"""

USER_PROFILE = {
    # Контакты
    "phone": "9000000000",
    "phone_formatted": "+7 (900) 000-00-00",
    "email": "you@example.com",
    "email_backup": "you.backup@example.com",

    # Имя
    "full_name": "Иванов Иван Иванович",
    "first_name": "Иван",
    "last_name": "Иванов",
    "middle_name": "Иванович",
    "first_name_en": "Ivan",
    "last_name_en": "Ivanov",

    # Никнеймы / логины
    "username": "yournick",
    "nickname": "yournick",

    # Сайты пользователя
    "website": "https://example.com",
}


def profile_to_prompt() -> str:
    """Текстовое представление профиля для промпта LLM"""
    return f"""=== ДАННЫЕ ПОЛЬЗОВАТЕЛЯ (используй при заполнении форм) ===
Телефон: {USER_PROFILE['phone']} (или {USER_PROFILE['phone_formatted']} — выбери формат, который требует поле)
Email основной: {USER_PROFILE['email']}
Email резервный: {USER_PROFILE['email_backup']}
ФИО: {USER_PROFILE['full_name']}
Имя: {USER_PROFILE['first_name']} / {USER_PROFILE['first_name_en']}
Фамилия: {USER_PROFILE['last_name']} / {USER_PROFILE['last_name_en']}
Отчество: {USER_PROFILE['middle_name']}
Никнейм/логин: {USER_PROFILE['username']}
Сайт: {USER_PROFILE['website']}

Правила:
- Выбирай значение по смыслу поля (name→имя, phone→телефон, email→email, username→ник)
- Если поле требует латиницу — используй английские варианты имени
- Если данных не хватает (пароль, карта, адрес, код из SMS) — НЕ выдумывай,
  запроси у пользователя через finish с reason "нужен X"
- Пароли не хранятся и не вводятся автоматически без явного указания в задаче"""
