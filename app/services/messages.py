import logging
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.cache import MessageCache
from app.errors import ConflictError, NotFoundError
from app.models import Author, Message
from app.schemas import MessageCreate, MessageOut, MessageUpdate, Page

log = logging.getLogger(__name__)

FK_VIOLATION = "23503"


def _not_found(message_id: uuid.UUID) -> NotFoundError:
    return NotFoundError(f"Message with ID '{message_id}' was not found.")


async def _load(session: AsyncSession, message_id: uuid.UUID) -> MessageOut | None:
    row = (
        await session.execute(
            select(Message).options(joinedload(Message.author)).where(Message.id == message_id)
        )
    ).scalar_one_or_none()
    return MessageOut.model_validate(row) if row else None


async def list_messages(session: AsyncSession, limit: int, offset: int) -> Page[MessageOut]:
    rows = (
        (
            await session.execute(
                select(Message)
                .options(joinedload(Message.author))
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = (await session.execute(select(func.count()).select_from(Message))).scalar_one()
    return Page[MessageOut](items=[MessageOut.model_validate(r) for r in rows], total_count=total)


async def get_message(
    session: AsyncSession, cache: MessageCache, message_id: uuid.UUID
) -> MessageOut:
    """Cache-aside: read the cache first; on a miss load from the DB and populate the cache."""
    cached = await cache.get(str(message_id))
    if cached is not None:
        return MessageOut.model_validate_json(cached)

    message = await _load(session, message_id)
    if message is None:
        raise _not_found(message_id)
    await cache.set(str(message_id), message.model_dump_json(by_alias=True))
    return message


async def create_message(session: AsyncSession, payload: MessageCreate) -> MessageOut:
    author = await session.get(Author, payload.author_id)
    if author is None:
        raise NotFoundError(f"Author with ID '{payload.author_id}' was not found.")

    message = Message(title=payload.title, content=payload.content, author_id=author.id)
    session.add(message)
    try:
        await session.flush()
    except IntegrityError as exc:  # the author was deleted between the check and the insert
        await session.rollback()
        if getattr(exc.orig, "sqlstate", None) == FK_VIOLATION:
            raise NotFoundError(f"Author with ID '{payload.author_id}' was not found.") from exc
        raise
    message.author = author
    out = MessageOut.model_validate(message)
    await session.commit()
    return out


async def update_message(
    session: AsyncSession,
    cache: MessageCache,
    message_id: uuid.UUID,
    payload: MessageUpdate,
) -> MessageOut:
    """Optimistic locking in a single statement: the row only changes if it is still at the
    version the caller read. Checking a server-side re-read instead would compare the row to
    itself and never catch a client acting on stale data."""
    values: dict[str, object] = {"content": payload.content, "version": Message.version + 1}
    if payload.title is not None:
        values["title"] = payload.title

    updated_id = (
        await session.execute(
            update(Message)
            .where(Message.id == message_id, Message.version == payload.version)
            .values(**values)
            .returning(Message.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()

    if updated_id is None:
        await session.rollback()
        # The caller's version is stale, so a cached copy may be too (a slow reader can
        # repopulate the cache with a pre-update row right after an update's eviction). Evict so
        # the next read reloads from the DB instead of handing out the stale version forever.
        await cache.evict(str(message_id))
        exists = await session.scalar(select(Message.id).where(Message.id == message_id))
        if exists is None:
            raise _not_found(message_id)
        raise ConflictError(
            f"Message with ID '{message_id}' has changed since version {payload.version} was "
            "read; refetch and retry."
        )

    out = await _load(session, message_id)
    await session.commit()
    await cache.evict(str(message_id))
    assert out is not None
    return out


async def delete_message(session: AsyncSession, cache: MessageCache, message_id: uuid.UUID) -> None:
    deleted = (
        await session.execute(delete(Message).where(Message.id == message_id).returning(Message.id))
    ).scalar_one_or_none()
    await session.commit()
    await cache.evict(str(message_id))
    if deleted is None:
        raise _not_found(message_id)
