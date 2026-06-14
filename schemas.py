"""Pydantic-схемы запросов API."""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TgAuthData(BaseModel):
    id: int
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str


class EmailReq(BaseModel):
    email: str


class ProfileData(BaseModel):
    email:      Optional[str] = None
    name:       str
    phone:      str
    city:       str
    linkedin:   str = ""
    experience: List[Dict[str, Any]]
    education:  List[Dict[str, Any]]
    skills:     str
    languages:  str


class MatchReq(BaseModel):
    email:       Optional[str] = None
    job_text:    str
    company:     str = ""
    job_url:     str = ""
    extra_hint:  str = ""


class GenerateFromProfileReq(BaseModel):
    email:       Optional[str] = None
    target_role: str = ""
    hint:        str = ""


class GenerateReq(BaseModel):
    email:      Optional[str] = None
    name:       str
    phone:      str
    city:       str
    linkedin:   str = ""
    target:     str
    hint:       str = ""
    experience: List[Dict[str, Any]]
    education:  List[Dict[str, Any]]
    skills:     str
    languages:  str


class PayReq(BaseModel):
    email: Optional[str] = None


class ResumeStatusReq(BaseModel):
    status: str


class ImproveReq(BaseModel):
    kind:    str        # "summary" | "bullets" | "skills"
    text:    str
    context: str = ""


class AnonymousPreviewReq(BaseModel):
    """Генерация без аккаунта — профиль передаётся инлайн, ничего не сохраняется."""
    kind:        str        # "match" | "general"
    profile:     dict
    job_text:    str = ""
    job_url:     str = ""
    target_role: str = ""
    hint:        str = ""
