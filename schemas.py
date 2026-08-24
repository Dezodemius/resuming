"""Pydantic-схемы запросов API."""
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

# ── Лимиты полей ────────────────────────────────────────────────────────────
# Резюме — текстовый документ на пару страниц, а не файл: лимиты ниже взяты с
# запасом на реальные данные (десятки-сотни символов для одной строки анкеты),
# но так, чтобы один аноним не смог накачать промпт до мегабайтов (issue про
# 10 МБ profile/job_text — см. AnonymousPreviewReq и client_max_body_size в
# nginx.conf). Списки (опыт/образование) ограничены количеством пунктов —
# длину каждого поля внутри пункта дополнительно режет prompts.py, так как
# сами пункты остаются словарями произвольной формы (см. комментарий у
# AnonymousPreviewReq.profile).
_NAME_MAX     = 200
_PHONE_MAX    = 50
_CITY_MAX     = 100
_LINKEDIN_MAX = 300
_TARGET_MAX   = 200
_TEXT_MAX     = 4000    # skills, languages — свободный текст в одну строку
_HINT_MAX     = 2000
_JOB_TEXT_MAX = 20_000  # с большим запасом к 3500, что реально уходит в промпт
_URL_MAX      = 2000
_COMPANY_MAX  = 200
_LIST_MAX     = 50      # пунктов опыта/образования — с запасом к реальной карьере
_KIND_MAX     = 20
_IMPROVE_TEXT_MAX = 20_000  # main.py уже режет на 10_000 своей 400-кой — этот
                             # предел выше её, чтобы не подменять понятное
                             # сообщение пользователю на голый 422, и работает
                             # как верхний backstop против совсем больших тел
_CODE_MAX     = 64
_COMMENT_MAX  = 500
_DATE_MAX     = 64
_EVENT_MAX    = 50
_STATUS_MAX   = 20

# Анонимный inline-профиль (AnonymousPreviewReq.profile) типизированной
# модели не имеет — см. её докстринг. Единственный способ ограничить размер —
# пройтись по значению вручную: общий вес сериализованного словаря плюс длина
# отдельной строки/список внутри него.
_PROFILE_MAX_BYTES = 50_000
_PROFILE_STR_MAX   = 5_000
_PROFILE_LIST_MAX  = 50
_PROFILE_MAX_DEPTH = 4


class EmailReq(BaseModel):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> Any:
        # Нормализуем ДО валидации формата: так пробелы по краям не роняют
        # EmailStr, а регистр приводится к тому же виду, что и _normalize_email
        # в main.py (Ivan@ya.ru из формы и ivan@ya.ru из OAuth — один аккаунт).
        return v.strip().lower() if isinstance(v, str) else v


class ProfileData(BaseModel):
    email:      Optional[str] = Field(None, max_length=254)
    name:       str = Field(..., max_length=_NAME_MAX)
    phone:      str = Field(..., max_length=_PHONE_MAX)
    city:       str = Field(..., max_length=_CITY_MAX)
    linkedin:   str = Field("", max_length=_LINKEDIN_MAX)
    experience: List[Dict[str, Any]] = Field(..., max_length=_LIST_MAX)
    education:  List[Dict[str, Any]] = Field(..., max_length=_LIST_MAX)
    skills:     str = Field(..., max_length=_TEXT_MAX)
    languages:  str = Field(..., max_length=_TEXT_MAX)


class MatchReq(BaseModel):
    email:       Optional[str] = Field(None, max_length=254)
    job_text:    str = Field(..., max_length=_JOB_TEXT_MAX)
    company:     str = Field("", max_length=_COMPANY_MAX)
    job_url:     str = Field("", max_length=_URL_MAX)
    extra_hint:  str = Field("", max_length=_HINT_MAX)


class GenerateFromProfileReq(BaseModel):
    email:       Optional[str] = Field(None, max_length=254)
    target_role: str = Field("", max_length=_TARGET_MAX)
    hint:        str = Field("", max_length=_HINT_MAX)


class GenerateReq(BaseModel):
    email:      Optional[str] = Field(None, max_length=254)
    name:       str = Field(..., max_length=_NAME_MAX)
    phone:      str = Field(..., max_length=_PHONE_MAX)
    city:       str = Field(..., max_length=_CITY_MAX)
    linkedin:   str = Field("", max_length=_LINKEDIN_MAX)
    target:     str = Field(..., max_length=_TARGET_MAX)
    hint:       str = Field("", max_length=_HINT_MAX)
    experience: List[Dict[str, Any]] = Field(..., max_length=_LIST_MAX)
    education:  List[Dict[str, Any]] = Field(..., max_length=_LIST_MAX)
    skills:     str = Field(..., max_length=_TEXT_MAX)
    languages:  str = Field(..., max_length=_TEXT_MAX)


class PayReq(BaseModel):
    email: Optional[str] = Field(None, max_length=254)


class ResumeStatusReq(BaseModel):
    status: str = Field(..., max_length=_STATUS_MAX)


class ImproveReq(BaseModel):
    kind:    str = Field(..., max_length=_KIND_MAX)  # "summary" | "bullets" | "skills"
    text:    str = Field(..., max_length=_IMPROVE_TEXT_MAX)
    context: str = Field("", max_length=_HINT_MAX)


def _clip_profile(value: Any, depth: int = 0) -> Any:
    """Рекурсивно обрезает строки/списки внутри анонимного profile.

    depth ограничивает глубину, чтобы искусственно вложенный JSON
    ({"a": {"a": {"a": ...}}}) не превратился в способ обойти обрезку.
    """
    if depth > _PROFILE_MAX_DEPTH:
        return None
    if isinstance(value, str):
        return value[:_PROFILE_STR_MAX]
    if isinstance(value, list):
        return [_clip_profile(v, depth + 1) for v in value[:_PROFILE_LIST_MAX]]
    if isinstance(value, dict):
        return {k: _clip_profile(v, depth + 1) for k, v in list(value.items())[:_PROFILE_LIST_MAX]}
    return value


class AnonymousPreviewReq(BaseModel):
    """Генерация без аккаунта — профиль передаётся инлайн, ничего не сохраняется.

    profile — намеренно dict, а не типизированная модель: main.py и
    prompts.py читают его через .get(...), и строгая схема с обязательными
    полями давала бы 422 на неполном анкетном профиле — то есть регрессию
    живой воронки для анонимов. Вместо типизации ограничиваем размер вручную:
    сериализованный словарь не длиннее _PROFILE_MAX_BYTES (иначе — отказ), а
    внутри — обрезаем длинные строки и списки (_clip_profile). Так исходный
    сценарий issue (10 МБ profile от анонима) закрыт без слома формы.
    """
    kind:        str = Field(..., max_length=_KIND_MAX)  # "match" | "general"
    profile:     dict
    job_text:    str = Field("", max_length=_JOB_TEXT_MAX)
    job_url:     str = Field("", max_length=_URL_MAX)
    target_role: str = Field("", max_length=_TARGET_MAX)
    hint:        str = Field("", max_length=_HINT_MAX)

    @field_validator("profile")
    @classmethod
    def _limit_profile(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False)) > _PROFILE_MAX_BYTES:
            raise ValueError("Профиль слишком большой")
        return _clip_profile(v)


class TrackReq(BaseModel):
    """Шаг воронки с лендинга. Имя события сверяется с белым списком в main."""
    event: str = Field(..., max_length=_EVENT_MAX)


class PromoActivateReq(BaseModel):
    code: str = Field(..., max_length=_CODE_MAX)


class PromoCreateReq(BaseModel):
    kind:       str = Field(..., max_length=_KIND_MAX)  # "pro_days" | "gen_pack" | "unlimited"
    value:      int
    max_uses:   int
    expires_at: Optional[str] = Field(None, max_length=_DATE_MAX)
    comment:    str = Field("", max_length=_COMMENT_MAX)
