import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import problem_responses
from app.schemas import (
    MAX_INT4,
    AuthorCreate,
    AuthorDetailOut,
    AuthorOut,
    AuthorUpdate,
    Page,
)
from app.services import authors as service

router = APIRouter(prefix="/authors", tags=["authors"])

Session = Annotated[AsyncSession, Depends(get_session)]

Limit = Annotated[int, Query(ge=1, le=200, description="Page size, 1-200.")]
Offset = Annotated[int, Query(ge=0, le=MAX_INT4, description="Rows to skip.")]


@router.get("", response_model=Page[AuthorOut], responses=problem_responses(400))
async def list_authors(session: Session, limit: Limit = 50, offset: Offset = 0) -> Page[AuthorOut]:
    return await service.list_authors(session, limit, offset)


@router.get(
    "/{id}",
    response_model=AuthorDetailOut,
    response_model_exclude_none=True,
    responses=problem_responses(400, 404),
)
async def get_author(
    id: uuid.UUID,
    session: Session,
    include: Annotated[Literal["messages"] | None, Query()] = None,
) -> AuthorDetailOut:
    return await service.get_author(session, id, include_messages=include == "messages")


@router.post(
    "", response_model=AuthorOut, status_code=201, responses=problem_responses(400, 409, 413)
)
async def create_author(payload: AuthorCreate, response: Response, session: Session) -> AuthorOut:
    created = await service.create_author(session, payload)
    response.headers["Location"] = f"/authors/{created.id}"
    return created


@router.patch("/{id}", response_model=AuthorOut, responses=problem_responses(400, 404, 409, 413))
async def update_author(id: uuid.UUID, payload: AuthorUpdate, session: Session) -> AuthorOut:
    return await service.update_author(session, id, payload)


@router.delete("/{id}", status_code=204, responses=problem_responses(400, 404, 409))
async def delete_author(id: uuid.UUID, session: Session) -> Response:
    await service.delete_author(session, id)
    return Response(status_code=204)
