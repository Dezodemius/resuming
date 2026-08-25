"""_match_prompt/_general_prompt/_generate_prompt: сборка промпта из данных.

Сами функции — почти целиком текст инструкции для модели (см. заголовок
prompts.py и tools/mutation_ignore.txt про config._login_methods_report:
дословную формулировку инструкции пинить тестами бессмысленно, она меняется
при любой правке текста). Но извлечение полей из словарей — реальная логика:
опечатка в имени ключа (`e.get('degree', ...)` -> `e.get('XXdegreeXX', ...)`)
молча роняет поле из промпта, и AI-модель никогда не увидит образование или
опыт кандидата. Эти тесты проверяют именно это: каждое поле профиля
действительно доходит до текста промпта.
"""
from prompts import (
    _FIELD_MAX,
    _HINT_MAX,
    _ITEM_MAX,
    _TEXT_MAX,
    _general_prompt,
    _generate_prompt,
    _match_prompt,
)
from schemas import GenerateReq


def _profile():
    return {
        "name": "Иван Тестовый",
        "city": "Уфа",
        "phone": "+7-900-000-00-00",
        "experience": [{
            "role": "Ведущий разработчик",
            "company": "ООО Ромашка",
            "period": "2020-2024",
            "desc": "Разрабатывал платёжный шлюз",
        }],
        "education": [{
            "degree": "Магистр прикладной математики",
            "institution": "УГАТУ",
            "year": "2019",
        }],
        "skills": "Python, PostgreSQL",
        "languages": "английский B2",
    }


_PROFILE_FIELDS = [
    "Иван Тестовый", "Уфа", "+7-900-000-00-00",
    "Ведущий разработчик", "ООО Ромашка", "2020-2024", "Разрабатывал платёжный шлюз",
    "Магистр прикладной математики", "УГАТУ", "2019",
    "Python, PostgreSQL", "английский B2",
]


def test_match_prompt_carries_every_profile_field():
    prompt = _match_prompt(_profile(), "Текст вакансии на 30+ символов для теста", "особые пожелания")
    for field in _PROFILE_FIELDS:
        assert field in prompt, f"поле {field!r} потерялось при сборке _match_prompt"
    assert "особые пожелания" in prompt
    assert "Текст вакансии на 30+ символов для теста" in prompt


def test_match_prompt_truncates_job_text_to_3500_chars():
    long_job = "А" * 4000
    prompt = _match_prompt(_profile(), long_job, "")
    assert "А" * 3500 in prompt
    assert "А" * 3501 not in prompt


def test_general_prompt_carries_every_profile_field():
    # phone сюда сознательно не входит — в отличие от _match_prompt, у
    # _general_prompt в шапке только "{name} | {city}" (см. prompts.py).
    fields = [f for f in _PROFILE_FIELDS if f != "+7-900-000-00-00"]
    prompt = _general_prompt(_profile(), "Backend-разработчик", "пожелание к резюме")
    for field in fields:
        assert field in prompt, f"поле {field!r} потерялось при сборке _general_prompt"
    assert "Backend-разработчик" in prompt
    assert "пожелание к резюме" in prompt


def test_general_prompt_asks_model_to_infer_role_when_not_given():
    """Логика (не текст): при пустом target_role должность не подставляется
    дословно — вместо неё указание определить её по опыту."""
    prompt = _general_prompt(_profile(), target_role="", hint="")
    assert "Backend" not in prompt


def test_general_prompt_uses_given_target_role_when_provided():
    prompt = _general_prompt(_profile(), target_role="Data Engineer", hint="")
    assert "Data Engineer" in prompt


def test_generate_prompt_carries_every_field():
    req = GenerateReq(
        name="Иван Тестовый", phone="+7-900-000-00-00", city="Уфа",
        target="Data Engineer", hint="пожелание тут",
        experience=[{"role": "Ведущий разработчик", "company": "ООО Ромашка",
                     "period": "2020-2024", "desc": "Разрабатывал платёжный шлюз"}],
        education=[{"degree": "Магистр прикладной математики", "institution": "УГАТУ", "year": "2019"}],
        skills="Python, PostgreSQL", languages="английский B2",
    )
    prompt = _generate_prompt(req)
    # city/phone у GenerateReq вообще не используются в шаблоне (см. prompts.py:
    # "Имя: {r.name} | Должность: {r.target} | Пожелания: {r.hint}") — модель их
    # никогда не увидит. Существующее поведение, не предмет этого теста.
    fields = [f for f in _PROFILE_FIELDS if f not in ("Уфа", "+7-900-000-00-00")]
    for field in fields + ["Data Engineer", "пожелание тут"]:
        assert field in prompt, f"поле {field!r} потерялось при сборке _generate_prompt"


# ── Пустой опыт/образование: fallback "не указан(о)" ────────────────────────
# or-fallback в exp/edu — реальная логика: замени "or" на "and", и при пустом
# списке в промпт уйдёт пустая строка вместо пометки "не указан(о)".

def test_match_prompt_marks_missing_experience_and_education():
    profile = _profile()
    profile["experience"] = []
    profile["education"] = []
    prompt = _match_prompt(profile, "Текст вакансии на 30+ символов для теста", "")
    assert "не указан" in prompt
    assert "не указано" in prompt
    # "не указан" — подстрока и внутри "XX  не указанXX": одного вхождения
    # текста мало, формулировка обязана быть дословной, без обёртки.
    assert "XX" not in prompt


def test_general_prompt_marks_missing_experience_and_education():
    profile = _profile()
    profile["experience"] = []
    profile["education"] = []
    prompt = _general_prompt(profile, "", "")
    assert "не указан" in prompt
    assert "не указано" in prompt
    assert "XX" not in prompt


def test_generate_prompt_marks_missing_experience_and_education():
    req = GenerateReq(
        name="Иван", phone="1", city="Уфа", target="Dev", hint="",
        experience=[], education=[], skills="Python", languages="RU",
    )
    prompt = _generate_prompt(req)
    assert "не указан" in prompt
    assert "не указано" in prompt
    assert "XX" not in prompt


# ── Частично заполненные записи: default у .get(), а не сам ключ ───────────
# Профиль приходит от клиента (анонимный инлайн-JSON или сохранённый), поля
# внутри одной записи опыта/образования могут отсутствовать по отдельности —
# .get(key, "") обязан отдавать пустую строку, а не React-стиля "None" в
# тексте промпта для модели.

def test_match_prompt_handles_experience_entry_missing_fields():
    profile = _profile()
    profile["experience"] = [{}]
    profile["education"] = [{}]
    prompt = _match_prompt(profile, "Текст вакансии на 30+ символов для теста", "")
    assert "None" not in prompt
    assert "XXXX" not in prompt


def test_match_prompt_handles_profile_without_experience_key_at_all():
    """Не просто пустой список — ключа experience/education в словаре нет
    вовсе (например анонимный профиль пришёл не полностью)."""
    profile = _profile()
    del profile["experience"]
    del profile["education"]
    prompt = _match_prompt(profile, "Текст вакансии на 30+ символов для теста", "")
    assert "не указан" in prompt
    assert "не указано" in prompt


def test_general_prompt_handles_experience_entry_missing_fields():
    profile = _profile()
    profile["experience"] = [{}]
    profile["education"] = [{}]
    prompt = _general_prompt(profile, "", "")
    assert "None" not in prompt
    assert "XXXX" not in prompt


def test_generate_prompt_handles_experience_entry_missing_fields():
    req = GenerateReq(
        name="Иван", phone="1", city="Уфа", target="Dev", hint="",
        experience=[{}], education=[{}], skills="Python", languages="RU",
    )
    prompt = _generate_prompt(req)
    assert "None" not in prompt
    assert "XXXX" not in prompt


# ── Профиль вообще без верхнеуровневых полей ────────────────────────────────

def test_match_prompt_handles_profile_missing_top_level_keys():
    prompt = _match_prompt({}, "Текст вакансии на 30+ символов для теста", "")
    assert "None" not in prompt
    assert "XXXX" not in prompt


def test_general_prompt_handles_profile_missing_top_level_keys():
    prompt = _general_prompt({}, "", "")
    assert "None" not in prompt
    assert "XXXX" not in prompt


# ── Несколько записей опыта/образования: разделитель join ──────────────────
# С одной записью "\n".join([x]) вернёт x независимо от разделителя — нужно
# минимум две, чтобы разделитель между ними вообще на что-то влиял.

def _profile_with_two_jobs():
    profile = _profile()
    profile["experience"] = [
        {"role": "Разработчик", "company": "Альфа", "period": "2018-2020", "desc": "A"},
        {"role": "Тимлид", "company": "Бета", "period": "2020-2024", "desc": "B"},
    ]
    profile["education"] = [
        {"degree": "Бакалавр", "institution": "ВУЗ1", "year": "2016"},
        {"degree": "Магистр", "institution": "ВУЗ2", "year": "2018"},
    ]
    return profile


def test_match_prompt_separates_multiple_entries_with_newline():
    prompt = _match_prompt(_profile_with_two_jobs(), "Текст вакансии на 30+ символов для теста", "")
    assert "Разработчик в Альфа (2018-2020): A\n  - Тимлид в Бета (2020-2024): B" in prompt
    assert "Бакалавр — ВУЗ1 (2016)\n  - Магистр — ВУЗ2 (2018)" in prompt


def test_general_prompt_separates_multiple_entries_with_newline():
    prompt = _general_prompt(_profile_with_two_jobs(), "", "")
    assert "Разработчик в Альфа (2018-2020): A\n  - Тимлид в Бета (2020-2024): B" in prompt
    assert "Бакалавр — ВУЗ1 (2016)\n  - Магистр — ВУЗ2 (2018)" in prompt


def test_generate_prompt_separates_multiple_entries_with_newline():
    req = GenerateReq(
        name="Иван", phone="1", city="Уфа", target="Dev", hint="",
        experience=[
            {"role": "Разработчик", "company": "Альфа", "period": "2018-2020", "desc": "A"},
            {"role": "Тимлид", "company": "Бета", "period": "2020-2024", "desc": "B"},
        ],
        education=[
            {"degree": "Бакалавр", "institution": "ВУЗ1", "year": "2016"},
            {"degree": "Магистр", "institution": "ВУЗ2", "year": "2018"},
        ],
        skills="Python", languages="RU",
    )
    prompt = _generate_prompt(req)
    assert "Разработчик в Альфа (2018-2020): A\n  - Тимлид в Бета (2020-2024): B" in prompt
    assert "Бакалавр — ВУЗ1 (2016)\n  - Магистр — ВУЗ2 (2018)" in prompt


# ── Значения по умолчанию в сигнатуре (не переданы вовсе) ───────────────────
# Отдельно от "передали пустую строку явно": сигнатура сама подставляет "" —
# у _general_prompt именно от этого зависит, сработает ли "определи сам".

def test_general_prompt_signature_defaults_to_empty_target_role_and_hint():
    prompt = _general_prompt(_profile())
    assert "определи сам по опыту" in prompt
    assert "Пожелания: \n" in prompt


def test_match_prompt_signature_defaults_extra_to_empty():
    prompt = _match_prompt(_profile(), "Текст вакансии на 30+ символов для теста")
    assert "Пожелания: >>>" in prompt


# ── Обрезка длины полей (_clip) ──────────────────────────────────────────────
# Вторая линия защиты после лимитов schemas.py (см. докстринг _clip в
# prompts.py): profile может прийти как свободный dict из анонимной ручки или
# из БД, сохранённый до появления лимитов, — schemas.py его не ограничивает.
# Каждое поле профиля пропускается через _clip(value, LIMIT); мутация
# LIMIT -> None превращает срез в str(value)[:None], то есть в отсутствие
# обрезки вовсе. Ловим это подстановкой заведомо длинной строки из одного
# символа на каждое поле и проверкой точной длины среза в готовом промпте.

def _assert_clipped(prompt: str, char: str, limit: int) -> None:
    assert char * limit in prompt, f"поле {char!r} должно попасть в промпт хотя бы усечённым до {limit}"
    assert char * (limit + 1) not in prompt, f"поле {char!r} обрезано неверно — длиннее {limit}"


def test_match_prompt_clips_every_field_to_its_limit():
    profile = {
        "name": "N" * (_FIELD_MAX + 50),
        "city": "T" * (_FIELD_MAX + 50),
        "phone": "H" * (_FIELD_MAX + 50),
        "experience": [{
            "role": "R" * (_FIELD_MAX + 50),
            "company": "C" * (_FIELD_MAX + 50),
            "period": "P" * (_FIELD_MAX + 50),
            "desc": "D" * (_ITEM_MAX + 50),
        }],
        "education": [{
            "degree": "G" * (_FIELD_MAX + 50),
            "institution": "I" * (_FIELD_MAX + 50),
            "year": "Y" * (_FIELD_MAX + 50),
        }],
        "skills": "S" * (_TEXT_MAX + 50),
        "languages": "L" * (_TEXT_MAX + 50),
    }
    prompt = _match_prompt(profile, "Текст вакансии", "E" * (_HINT_MAX + 50))
    for char, limit in [
        ("N", _FIELD_MAX), ("T", _FIELD_MAX), ("H", _FIELD_MAX),
        ("R", _FIELD_MAX), ("C", _FIELD_MAX), ("P", _FIELD_MAX), ("D", _ITEM_MAX),
        ("G", _FIELD_MAX), ("I", _FIELD_MAX), ("Y", _FIELD_MAX),
        ("S", _TEXT_MAX), ("L", _TEXT_MAX), ("E", _HINT_MAX),
    ]:
        _assert_clipped(prompt, char, limit)


def test_general_prompt_clips_every_field_to_its_limit():
    profile = {
        "name": "N" * (_FIELD_MAX + 50),
        "city": "T" * (_FIELD_MAX + 50),
        "experience": [{
            "role": "R" * (_FIELD_MAX + 50),
            "company": "C" * (_FIELD_MAX + 50),
            "period": "P" * (_FIELD_MAX + 50),
            "desc": "D" * (_ITEM_MAX + 50),
        }],
        "education": [{
            "degree": "G" * (_FIELD_MAX + 50),
            "institution": "I" * (_FIELD_MAX + 50),
            "year": "Y" * (_FIELD_MAX + 50),
        }],
        "skills": "S" * (_TEXT_MAX + 50),
        "languages": "L" * (_TEXT_MAX + 50),
    }
    prompt = _general_prompt(
        profile,
        target_role="M" * (_FIELD_MAX + 50),
        hint="E" * (_HINT_MAX + 50),
    )
    for char, limit in [
        ("N", _FIELD_MAX), ("T", _FIELD_MAX),
        ("R", _FIELD_MAX), ("C", _FIELD_MAX), ("P", _FIELD_MAX), ("D", _ITEM_MAX),
        ("G", _FIELD_MAX), ("I", _FIELD_MAX), ("Y", _FIELD_MAX),
        ("S", _TEXT_MAX), ("L", _TEXT_MAX), ("E", _HINT_MAX), ("M", _FIELD_MAX),
    ]:
        _assert_clipped(prompt, char, limit)


def test_generate_prompt_clips_every_field_to_its_limit():
    # У GenerateReq верхнеуровневые поля (name/target/hint/skills/languages)
    # ограничены схемой короче, чем _FIELD_MAX/_TEXT_MAX/_HINT_MAX в
    # prompts.py (schemas._NAME_MAX=200 < prompts._FIELD_MAX=300) — легитимно
    # созданный запрос лимит _clip физически не достанет. Присваиваем поля
    # напрямую после создания (validate_assignment у модели не включён) —
    # так же, как в проде до появления лимитов мог быть сохранён профиль.
    req = GenerateReq(
        name="x", phone="1", city="y", target="t", hint="h",
        experience=[{
            "role": "R" * (_FIELD_MAX + 50),
            "company": "C" * (_FIELD_MAX + 50),
            "period": "P" * (_FIELD_MAX + 50),
            "desc": "D" * (_ITEM_MAX + 50),
        }],
        education=[{
            "degree": "G" * (_FIELD_MAX + 50),
            "institution": "I" * (_FIELD_MAX + 50),
            "year": "Y" * (_FIELD_MAX + 50),
        }],
        skills="s", languages="l",
    )
    req.name = "N" * (_FIELD_MAX + 50)
    req.target = "M" * (_FIELD_MAX + 50)
    req.hint = "E" * (_HINT_MAX + 50)
    req.skills = "S" * (_TEXT_MAX + 50)
    req.languages = "L" * (_TEXT_MAX + 50)

    prompt = _generate_prompt(req)
    for char, limit in [
        ("R", _FIELD_MAX), ("C", _FIELD_MAX), ("P", _FIELD_MAX), ("D", _ITEM_MAX),
        ("G", _FIELD_MAX), ("I", _FIELD_MAX), ("Y", _FIELD_MAX),
        ("N", _FIELD_MAX), ("M", _FIELD_MAX), ("E", _HINT_MAX),
        ("S", _TEXT_MAX), ("L", _TEXT_MAX),
    ]:
        _assert_clipped(prompt, char, limit)
