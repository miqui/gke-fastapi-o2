import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import MessageCache, get_cache
from app.db import get_session
from app.errors import problem_responses
from app.schemas import MAX_INT4, MessageCreate, MessageOut, MessageUpdate, Page
from app.services import messages as service

router = APIRouter(prefix="/messages", tags=["messages"])

Session = Annotated[AsyncSession, Depends(get_session)]
Cache = Annotated[MessageCache, Depends(get_cache)]

Limit = Annotated[int, Query(ge=1, le=200, description="Page size, 1-200.")]
Offset = Annotated[int, Query(ge=0, le=MAX_INT4, description="Rows to skip.")]


@router.get("", response_model=Page[MessageOut], responses=problem_responses(400))
async def list_messages(
    session: Session, limit: Limit = 50, offset: Offset = 0
) -> Page[MessageOut]:
    return await service.list_messages(session, limit, offset)


@router.get("/{id}", response_model=MessageOut, responses=problem_responses(400, 404))
async def get_message(id: uuid.UUID, session: Session, cache: Cache) -> MessageOut:
    return await service.get_message(session, cache, id)


@router.post(
    "", response_model=MessageOut, status_code=201, responses=problem_responses(400, 404, 413)
)
async def create_message(
    payload: MessageCreate, response: Response, session: Session
) -> MessageOut:
    created = await service.create_message(session, payload)
    response.headers["Location"] = f"/messages/{created.id}"
    return created


@router.patch("/{id}", response_model=MessageOut, responses=problem_responses(400, 404, 409, 413))
async def update_message(
    id: uuid.UUID, payload: MessageUpdate, session: Session, cache: Cache
) -> MessageOut:
    return await service.update_message(session, cache, id, payload)


@router.delete("/{id}", status_code=204, responses=problem_responses(400, 404))
async def delete_message(id: uuid.UUID, session: Session, cache: Cache) -> Response:
    await service.delete_message(session, cache, id)
    return Response(status_code=204)
