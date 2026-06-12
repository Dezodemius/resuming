"""Регрессионный прогон промптов main.py через Ollama на эталонных данных.

Импортирует _match_prompt / _general_prompt / _generate_prompt и call_ai()
прямо из main.py, чтобы eval никогда не расходился с продом. Для каждого
промпта валидирует, что ответ модели — JSON с обязательными ключами схемы.

Запуск:  python .claude/skills/prompt-eval/eval.py
Env:     OLLAMA_URL / OLLAMA_MODEL — те же дефолты, что в main.py.
Exit 1, если хотя бы один промпт FAIL.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from fastapi import HTTPException

SKILL_DIR = Path(__file__).resolve().parent
REPO_ROOT = SKILL_DIR.parents[2]

# Импорт main.py безопасен, но имеет side-эффекты уровня модуля:
# load_dotenv(), os.makedirs(DATA_DIR), os.makedirs("static") относительно cwd,
# Jinja2Templates("templates"). БД при импорте НЕ создаётся (init_db — в lifespan).
# Поэтому: каталог данных — во временную папку, cwd — корень репозитория.
os.environ.setdefault("SECRET_KEY", "eval")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="prompt-eval-")
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

import main  # noqa: E402

# Windows-консоль по умолчанию не UTF-8 — иначе кириллица в выводе ломается
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

FIXTURES = SKILL_DIR / "fixtures"


def load_profile() -> dict:
    """Парсит fixtures/resume.txt в profile-словарь того же вида, что хранится в БД."""
    profile: dict = {"experience": [], "education": []}
    section = None
    current: dict | None = None
    simple = {
        "ИМЯ": "name", "ГОРОД": "city", "ТЕЛЕФОН": "phone", "EMAIL": "email",
        "НАВЫКИ": "skills", "ЯЗЫКИ": "languages", "ЖЕЛАЕМАЯ ДОЛЖНОСТЬ": "target_role",
    }
    for line in (FIXTURES / "resume.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "ОПЫТ:":
            section, current = "experience", None
            continue
        if stripped == "ОБРАЗОВАНИЕ:":
            section, current = "education", None
            continue
        if section is None:
            key, _, value = stripped.partition(":")
            if key.strip().upper() in simple:
                profile[simple[key.strip().upper()]] = value.strip()
            continue
        if stripped.startswith("- "):
            parts = [p.strip() for p in stripped[2:].split("|")]
            if section == "experience":
                current = {"role": parts[0], "company": parts[1], "period": parts[2], "desc": ""}
            else:
                current = {"degree": parts[0], "institution": parts[1], "year": parts[2]}
            profile[section].append(current)
        elif current is not None and section == "experience":
            current["desc"] = (current["desc"] + " " + stripped).strip()
    return profile


# Обязательные ключи JSON-схемы, которую требуют все три промпта
# (см. блок "JSON ТОЛЬКО" в main.py). ats_keywords — только у _match_prompt.
SCHEMA = {
    "name": str,
    "contact": dict,
    "target_role": str,
    "summary": str,
    "experience": list,
    "education": list,
    "skills": dict,
    "languages": list,
}
EXPERIENCE_KEYS = ("company", "role", "period", "bullets")
EDUCATION_KEYS = ("institution", "degree", "year")


def validate(data: dict, require_ats: bool) -> list[str]:
    errors = []
    schema = dict(SCHEMA)
    if require_ats:
        schema["ats_keywords"] = list
    for key, typ in schema.items():
        if key not in data:
            errors.append(f"нет ключа '{key}'")
        elif not isinstance(data[key], typ):
            errors.append(f"'{key}' должен быть {typ.__name__}, получен {type(data[key]).__name__}")
    for i, e in enumerate(data.get("experience") or []):
        if not isinstance(e, dict):
            errors.append(f"experience[{i}] не объект")
            continue
        for k in EXPERIENCE_KEYS:
            if k not in e:
                errors.append(f"experience[{i}] без ключа '{k}'")
        if not isinstance(e.get("bullets"), list) or not e.get("bullets"):
            errors.append(f"experience[{i}].bullets — не непустой список")
    for i, e in enumerate(data.get("education") or []):
        if not isinstance(e, dict):
            errors.append(f"education[{i}] не объект")
            continue
        for k in EDUCATION_KEYS:
            if k not in e:
                errors.append(f"education[{i}] без ключа '{k}'")
    return errors


def run_case(label: str, prompt: str, require_ats: bool) -> bool:
    t0 = time.monotonic()
    try:
        raw = asyncio.run(main.call_ai(prompt))
    except HTTPException as e:
        print(f"FAIL  {label}  ({time.monotonic() - t0:.1f}s)  запрос не выполнен: {e.detail}")
        return False
    elapsed = time.monotonic() - t0
    try:
        data = main._parse_ai(raw)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"FAIL  {label}  ({elapsed:.1f}s)  ответ не парсится как JSON: {e}")
        print(f"      первые 500 символов ответа:\n{raw[:500]}")
        return False
    if not isinstance(data, dict):
        print(f"FAIL  {label}  ({elapsed:.1f}s)  JSON — не объект, а {type(data).__name__}")
        print(f"      первые 500 символов ответа:\n{raw[:500]}")
        return False
    errors = validate(data, require_ats)
    if errors:
        print(f"FAIL  {label}  ({elapsed:.1f}s)  схема нарушена:")
        for err in errors:
            print(f"      - {err}")
        print(f"      первые 500 символов ответа:\n{raw[:500]}")
        return False
    print(f"PASS  {label}  ({elapsed:.1f}s)")
    return True


def build_cases() -> list[tuple[str, str, bool]]:
    profile = load_profile()
    vacancy = (FIXTURES / "vacancy.txt").read_text(encoding="utf-8")
    gen_req = main.GenerateReq(
        name=profile.get("name", ""),
        phone=profile.get("phone", ""),
        city=profile.get("city", ""),
        target=profile.get("target_role", ""),
        hint="",
        experience=profile["experience"],
        education=profile["education"],
        skills=profile.get("skills", ""),
        languages=profile.get("languages", ""),
    )
    return [
        ("_match_prompt   ", main._match_prompt(profile, vacancy), True),
        ("_general_prompt ", main._general_prompt(profile, profile.get("target_role", "")), False),
        ("_generate_prompt", main._generate_prompt(gen_req), False),
    ]


def main_eval() -> int:
    print(f"Ollama: {main.OLLAMA_URL}  модель: {main.MODEL}")
    failed = 0
    for label, prompt, require_ats in build_cases():
        if not run_case(label, prompt, require_ats):
            failed += 1
    print(f"\nИтог: {3 - failed}/3 PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main_eval())
