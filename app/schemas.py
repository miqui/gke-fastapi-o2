"""Pydantic request/response models. JSON bodies are camelCase; the DB stays snake_case.

Validation rules: required strings are trimmed, must be non-blank and within their max length;
optional strings (PATCH) are unchanged when absent or null and must pass the same checks when
present. Failures are collected across all fields and mapped to problem+json `invalidParams`
by app/errors.py.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic.alias_generators import to_camel

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_INT4 = 2_147_483_647

TITLE_MAX = 100
CONTENT_MAX = 1000
NAME_MAX = 50
EMAIL_MAX = 100


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


def _clean(value: str, name: str, max_length: int) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{name} is required and cannot be blank")
    if len(trimmed) > max_length:
        raise ValueError(f"{name} cannot exceed {max_length} characters")
    return trimmed


def _clean_email(value: str, name: str, max_length: int) -> str:
    trimmed = _clean(value, name, max_length)
    if not EMAIL_RE.match(trimmed):
        raise ValueError(f"{name} must be a valid email address")
    return trimmed


_LIMITS = {"title": TITLE_MAX, "content": CONTENT_MAX, "name": NAME_MAX, "email": EMAIL_MAX}


class _Validated(ApiModel):
    @field_validator("title", "content", "name", "email", check_fields=False)
    @classmethod
    def _check_string(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:  # optional PATCH field: null/absent means unchanged
            return None
        name = info.field_name or "value"
        if name == "email":
            return _clean_email(value, name, _LIMITS[name])
        return _clean(value, name, _LIMITS[name])


# ---- requests -------------------------------------------------------------------------------


class MessageCreate(_Validated):
    title: str
    content: str
    author_id: uuid.UUID


class MessageUpdate(_Validated):
    title: str | None = None
    content: str
    # The version the caller read; the update only applies if the row is still at it.
    version: int = Field(ge=0, le=MAX_INT4)


class AuthorCreate(_Validated):
    name: str
    email: str


class AuthorUpdate(_Validated):
    name: str | None = None
    email: str | None = None


# ---- responses ------------------------------------------------------------------------------


class AuthorOut(ApiModel):
    id: uuid.UUID
    name: str
    email: str
    created_at: datetime


class MessageSummary(ApiModel):
    """A message without its author (used inside an author's `messages`)."""

    id: uuid.UUID
    title: str
    content: str
    created_at: datetime
    version: int


class MessageOut(MessageSummary):
    author: AuthorOut


class AuthorDetailOut(AuthorOut):
    # Only present with ?include=messages.
    messages: list[MessageSummary] | None = None


class Page[T](ApiModel):
    items: list[T]
    total_count: int
