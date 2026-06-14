"""Промпты для Ollama: строгий JSON-формат резюме.

Три сценария: адаптация под вакансию (_match_prompt), универсальное резюме из
профиля (_general_prompt) и генерация из инлайн-данных формы (_generate_prompt).
"""
from schemas import GenerateReq


def _match_prompt(profile: dict, job_text: str, extra: str = "") -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in profile.get("experience", [])) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in profile.get("education", [])) or "  не указано"
    return f"""Ты — ведущий HR-консультант. Адаптируй резюме под конкретную вакансию.

ПРОФИЛЬ: {profile.get('name','')} | {profile.get('city','')} | {profile.get('phone','')}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {profile.get('skills','')} | Языки: {profile.get('languages','')}
Пожелания: {extra}

ВАКАНСИЯ:\n{job_text[:3500]}

ЗАДАЧИ: извлеки ключевые требования, выбери релевантный опыт, вплети ключевые слова ATS, напиши точный summary.
НЕ выдумывай навыков которых нет в профиле.

JSON ТОЛЬКО (без markdown):
{{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."],"ats_keywords":["..."]}}"""


def _general_prompt(profile: dict, target_role: str = "", hint: str = "") -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in profile.get("experience", [])) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in profile.get("education", [])) or "  не указано"
    role_line = f"Желаемая должность: {target_role}" if target_role else "Желаемая должность: определи сам по опыту"
    return f"""Ты — ведущий HR-консультант. Создай универсальное профессиональное резюме.

ПРОФИЛЬ: {profile.get('name','')} | {profile.get('city','')}
{role_line} | Пожелания: {hint}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {profile.get('skills','')} | Языки: {profile.get('languages','')}

Включи весь опыт, 3–5 bullet-points с достижениями, широкий summary, сгруппируй навыки.

JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""


def _generate_prompt(r: GenerateReq) -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in r.experience) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in r.education) or "  не указано"
    return f"""Ты — HR-консультант. Создай резюме.
Имя: {r.name} | Должность: {r.target} | Пожелания: {r.hint}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {r.skills} | Языки: {r.languages}
JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""
