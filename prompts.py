"""Промпты для Ollama: строгий JSON-формат резюме.

Три сценария: адаптация под вакансию (_match_prompt), универсальное резюме из
профиля (_general_prompt) и генерация из инлайн-данных формы (_generate_prompt).
"""
from schemas import GenerateReq

# Вторая линия защиты после лимитов в schemas.py: там режется тело запроса,
# здесь — то, что реально уходит в модель. Нужна отдельно, потому что profile
# сюда попадает и из БД (сохранён до появления лимитов в schemas.py), и из
# анонимной ручки (там profile — свободный dict, поля внутри experience/
# education вообще не типизированы, см. AnonymousPreviewReq в schemas.py).
_FIELD_MAX = 300   # имя/город/телефон/линкедин/роль/компания/период
_ITEM_MAX  = 600   # описание одного пункта опыта/образования
_TEXT_MAX  = 4000  # навыки/языки
_HINT_MAX  = 2000
_LIST_MAX  = 50    # пунктов опыта/образования


def _clip(value, limit: int) -> str:
    """К строке и обрезка по длине — value может быть чем угодно (profile
    внутри AnonymousPreviewReq не типизирован дальше словаря)."""
    return str(value)[:limit]


def _match_prompt(profile: dict, job_text: str, extra: str = "") -> str:
    exp = "\n".join(f"  - {_clip(e.get('role',''), _FIELD_MAX)} в {_clip(e.get('company',''), _FIELD_MAX)} ({_clip(e.get('period',''), _FIELD_MAX)}): {_clip(e.get('desc',''), _ITEM_MAX)}" for e in profile.get("experience", [])[:_LIST_MAX]) or "  не указан"
    edu = "\n".join(f"  - {_clip(e.get('degree',''), _FIELD_MAX)} — {_clip(e.get('institution',''), _FIELD_MAX)} ({_clip(e.get('year',''), _FIELD_MAX)})" for e in profile.get("education", [])[:_LIST_MAX]) or "  не указано"
    return f"""Ты — ведущий HR-консультант. Адаптируй резюме под конкретную вакансию.

ПРОФИЛЬ: {_clip(profile.get('name',''), _FIELD_MAX)} | {_clip(profile.get('city',''), _FIELD_MAX)} | {_clip(profile.get('phone',''), _FIELD_MAX)}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {_clip(profile.get('skills',''), _TEXT_MAX)} | Языки: {_clip(profile.get('languages',''), _TEXT_MAX)}
Пожелания: {_clip(extra, _HINT_MAX)}

ВАКАНСИЯ:\n{job_text[:3500]}

ЗАДАЧИ: извлеки ключевые требования, выбери релевантный опыт, вплети ключевые слова ATS, напиши точный summary.
НЕ выдумывай навыков которых нет в профиле.

JSON ТОЛЬКО (без markdown):
{{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."],"ats_keywords":["..."]}}"""


def _general_prompt(profile: dict, target_role: str = "", hint: str = "") -> str:
    exp = "\n".join(f"  - {_clip(e.get('role',''), _FIELD_MAX)} в {_clip(e.get('company',''), _FIELD_MAX)} ({_clip(e.get('period',''), _FIELD_MAX)}): {_clip(e.get('desc',''), _ITEM_MAX)}" for e in profile.get("experience", [])[:_LIST_MAX]) or "  не указан"
    edu = "\n".join(f"  - {_clip(e.get('degree',''), _FIELD_MAX)} — {_clip(e.get('institution',''), _FIELD_MAX)} ({_clip(e.get('year',''), _FIELD_MAX)})" for e in profile.get("education", [])[:_LIST_MAX]) or "  не указано"
    target_role = _clip(target_role, _FIELD_MAX)
    role_line = f"Желаемая должность: {target_role}" if target_role else "Желаемая должность: определи сам по опыту"
    return f"""Ты — ведущий HR-консультант. Создай универсальное профессиональное резюме.

ПРОФИЛЬ: {_clip(profile.get('name',''), _FIELD_MAX)} | {_clip(profile.get('city',''), _FIELD_MAX)}
{role_line} | Пожелания: {_clip(hint, _HINT_MAX)}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {_clip(profile.get('skills',''), _TEXT_MAX)} | Языки: {_clip(profile.get('languages',''), _TEXT_MAX)}

Включи весь опыт, 3–5 bullet-points с достижениями, широкий summary, сгруппируй навыки.

JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""


def _generate_prompt(r: GenerateReq) -> str:
    # r уже прошёл лимиты схемы (schemas.GenerateReq), но experience/education —
    # список словарей произвольной формы (Dict[str, Any]): длину списка схема
    # ограничила, а длину полей внутри каждого пункта — нет, режем здесь.
    exp = "\n".join(f"  - {_clip(e.get('role',''), _FIELD_MAX)} в {_clip(e.get('company',''), _FIELD_MAX)} ({_clip(e.get('period',''), _FIELD_MAX)}): {_clip(e.get('desc',''), _ITEM_MAX)}" for e in r.experience[:_LIST_MAX]) or "  не указан"
    edu = "\n".join(f"  - {_clip(e.get('degree',''), _FIELD_MAX)} — {_clip(e.get('institution',''), _FIELD_MAX)} ({_clip(e.get('year',''), _FIELD_MAX)})" for e in r.education[:_LIST_MAX]) or "  не указано"
    return f"""Ты — HR-консультант. Создай резюме.
Имя: {_clip(r.name, _FIELD_MAX)} | Должность: {_clip(r.target, _FIELD_MAX)} | Пожелания: {_clip(r.hint, _HINT_MAX)}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {_clip(r.skills, _TEXT_MAX)} | Языки: {_clip(r.languages, _TEXT_MAX)}
JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""
