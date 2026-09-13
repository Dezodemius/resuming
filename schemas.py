"""Pydantic-схемы запросов API."""
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

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
# Промокод: дней Pro либо генераций в пачке (PromoCreateReq), и число
# активаций. Потолок — защита от опечатки в админке: 10 лет Pro (столько же
# генераций в пачке) и 100 000 активаций перекрывают любую акцию.
_PROMO_VALUE_MAX = 3650
_PROMO_USES_MAX  = 100_000
# Дней Pro / генераций в пачке, которые дев-стенд выдаёт себе сам (DevGrantReq).
# Верхняя граница здесь только от опечатки в служебной форме.
_DEV_VALUE_MAX = 10_000

# Виды промокодов, которые умеет выдавать админка и разбирать активация.
PROMO_KINDS = frozenset({"pro_days", "gen_pack", "unlimited"})

# Анонимный inline-профиль (AnonymousPreviewReq.profile) типизированной
# модели не имеет — см. её докстринг. Единственный способ ограничить размер —
# пройтись по значению вручную: общий вес сериализованного словаря плюс длина
# отдельной строки/список внутри него.
_PROFILE_MAX_BYTES = 50_000
_PROFILE_STR_MAX   = 5_000
_PROFILE_LIST_MAX  = 50
_PROFILE_MAX_DEPTH = 4

# Публичный предел единого нормализованного источника вакансии. Его импортируют
# fetch/storage/comparison paths, чтобы 20 000 не разъехались по файлам.
JOB_TEXT_MAX = _JOB_TEXT_MAX


class EmailReq(BaseModel):
    email: EmailStr
    # Явное действие, а не текст около кнопки. Старый клиент по умолчанию
    # получает 400 от /auth/email/request и не может начать регистрацию.
    terms_accepted: bool = False

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
    job_title:   str = Field("", max_length=_TARGET_MAX)
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


class SaveResumeReq(BaseModel):
    """Сохранение результата анонимной генерации после входа.

    job_snippet оставлен только для обратной совместимости со старым клиентом;
    доверенным полным источником считается исключительно job_text.
    """
    resume_data: Dict[str, Any]
    kind: Literal["general", "matched"] = "general"
    company_name: str = Field("", max_length=_COMPANY_MAX)
    job_url: str = Field("", max_length=_URL_MAX)
    job_text: str = Field("", max_length=_JOB_TEXT_MAX)
    job_snippet: str = Field("", max_length=300)

    @field_validator("resume_data")
    @classmethod
    def _non_empty_resume(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if not value:
            raise ValueError("Нет данных резюме")
        return value


class ProfileComparisonRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requirement: str = Field(..., min_length=1, max_length=300)
    importance: Literal["required", "preferred"]
    status: Literal["matched", "partial", "not_found"]
    vacancy_refs: List[str] = Field(..., min_length=1, max_length=5)
    profile_refs: List[str] = Field(default_factory=list, max_length=5)
    gap_explanation: Optional[str] = Field(None, max_length=400)

    @field_validator("requirement")
    @classmethod
    def _normalize_requirement(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("Пустое требование")
        return value

    @field_validator("vacancy_refs", "profile_refs")
    @classmethod
    def _unique_refs(cls, refs: List[str]) -> List[str]:
        if len(refs) != len(set(refs)):
            raise ValueError("Повторяющиеся ссылки на источник")
        return refs

    @model_validator(mode="after")
    def _consistent_status(self):
        gap = (self.gap_explanation or "").strip()
        if self.status == "matched" and (not self.profile_refs or gap):
            raise ValueError("matched требует profile_refs и не допускает gap_explanation")
        if self.status == "partial" and (not self.profile_refs or not gap):
            raise ValueError("partial требует profile_refs и gap_explanation")
        if self.status == "not_found" and (self.profile_refs or not gap):
            raise ValueError("not_found требует пустые profile_refs и gap_explanation")
        self.gap_explanation = gap or None
        return self


class ProfileComparisonAiResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    requirements: List[ProfileComparisonRequirement] = Field(..., min_length=1, max_length=25)

    @model_validator(mode="after")
    def _unique_requirements(self):
        normalized = [" ".join(item.requirement.split()).casefold() for item in self.requirements]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Повторяющиеся требования")
        return self


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
    # Согласие на передачу данных AI-провайдеру. Аккаунта, где его можно было
    # бы хранить, у анонима нет, поэтому подтверждение едет в самом запросе.
    # По умолчанию False: старый клиент из кеша браузера получит отказ, а не
    # молча отправит данные наружу без подтверждения.
    consent:     bool = False
    consent_rev: str = Field("", max_length=32)
    consent_hash: str = Field("", max_length=64)

    @field_validator("profile")
    @classmethod
    def _limit_profile(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False)) > _PROFILE_MAX_BYTES:
            raise ValueError("Профиль слишком большой")
        return _clip_profile(v)


class TrackReq(BaseModel):
    """Шаг воронки с лендинга. Имя события сверяется с белым списком в main."""
    event: str = Field(..., max_length=_EVENT_MAX)


class AiConsentReq(BaseModel):
    """Точная редакция документа, которую пользователь видел в браузере."""
    document_rev: str = Field(..., min_length=1, max_length=32)
    document_hash: str = Field(..., min_length=64, max_length=64)


class SiteConsentReq(BaseModel):
    """Единственная необязательная категория браузерного хранения сейчас."""
    choice: str = Field(..., pattern="^(analytics|necessary)$")


class PromoActivateReq(BaseModel):
    code: str = Field(..., max_length=_CODE_MAX)


class PromoDeactivateReq(BaseModel):
    """Тело /api/admin/promo/deactivate с типизированным кодом."""
    code: str = Field(..., max_length=_CODE_MAX)


class DevLoginReq(EmailReq):
    """Вход на дев-стенде: аккаунт заводится по почте, как при magic-ссылке,
    только без самой ссылки. Отдельный класс, а не EmailReq на месте вызова, —
    чтобы служебная ручка не выглядела в коде как обычная почтовая форма."""


class DevGrantReq(BaseModel):
    """Что выдать текущему аккаунту на дев-стенде.

    Набор значений plan проверяет main.py — так же, как kind у промокода:
    перечень живёт рядом с обработкой, а не в двух местах сразу.
    """
    plan:  str = Field(..., max_length=_KIND_MAX)
    # Дней Pro либо генераций в пачке; None — значение по умолчанию из config.
    value: Optional[int] = Field(None, ge=0, le=_DEV_VALUE_MAX)


class PromoCreateReq(BaseModel):
    # value и max_uses раньше были голыми int. Отрицательный gen_pack уводил
    # paid_left в минус (а `free_left + paid_left` в _deduct — вместе с ним:
    # аккаунт переставал генерировать вовсе), а pro_days с числом больше
    # ~999999999 ронял активацию в OverflowError уже ПОСЛЕ того, как
    # использование кода засчитано, — код сгорал впустую.
    kind:       str = Field(..., max_length=_KIND_MAX)  # "pro_days" | "gen_pack" | "unlimited"
    value:      int = Field(..., ge=0, le=_PROMO_VALUE_MAX)
    max_uses:   int = Field(..., ge=1, le=_PROMO_USES_MAX)
    expires_at: Optional[str] = Field(None, max_length=_DATE_MAX)
    comment:    str = Field("", max_length=_COMMENT_MAX)

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        # Тот же список стоит CHECK-ограничением в схеме promo_codes, но
        # ограничение ловит опечатку («pro_dyas») уже в sqlite — то есть
        # необработанным IntegrityError и 500-й в ответ. Здесь она становится
        # понятной 422 и не доходит до базы. Ограничение при этом остаётся
        # вторым рубежом: на базе, созданной до его появления, CREATE TABLE
        # IF NOT EXISTS его не добавит (см. CLAUDE.md про миграции).
        if v not in PROMO_KINDS:
            raise ValueError(f"kind должен быть одним из: {', '.join(sorted(PROMO_KINDS))}")
        return v
