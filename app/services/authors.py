import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ConflictError, NotFoundError
from app.models import Author
from app.schemas import (
    AuthorCreate,
    AuthorDetailOut,
    AuthorOut,
    AuthorUpdate,
    MessageSummary,
    Page,
)

UNIQUE_VIOLATION = "23505"
FK_VIOLATION = "23503"


def _not_found(author_id: uuid.UUID) -> NotFoundError:
    return NotFoundError(f"Author with ID '{author_id}' was not found.")


def _sqlstate(exc: IntegrityError) -> str | None:
    return getattr(exc.orig, "sqlstate", None)


def _duplicate_email(email: str) -> ConflictError:
    return ConflictError(f"Author with email '{email}' already exists.")


async def list_authors(session: AsyncSession, limit: int, offset: int) -> Page[AuthorOut]:
    rows = (
        (
            await session.execute(
                select(Author)
                .order_by(Author.created_at.asc(), Author.id.asc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    total = (await session.execute(select(func.count()).select_from(Author))).scalar_one()
    return Page[AuthorOut](items=[AuthorOut.model_validate(r) for r in rows], total_count=total)


async def get_author(
    session: AsyncSession, author_id: uuid.UUID, include_messages: bool
) -> AuthorDetailOut:
    query = select(Author).where(Author.id == author_id)
    if include_messages:
        query = query.options(selectinload(Author.messages))
    author = (await session.execute(query)).scalar_one_or_none()
    if author is None:
        raise _not_found(author_id)

    # `messages` is lazy="raise" and only loaded with ?include=messages, so build the response
    # explicitly rather than validating the ORM object.
    messages = (
        [MessageSummary.model_validate(m) for m in author.messages] if include_messages else None
    )
    return AuthorDetailOut(**AuthorOut.model_validate(author).model_dump(), messages=messages)


async def create_author(session: AsyncSession, payload: AuthorCreate) -> AuthorOut:
    author = Author(name=payload.name, email=payload.email)
    session.add(author)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        if _sqlstate(exc) == UNIQUE_VIOLATION:
            raise _duplicate_email(payload.email) from exc
        raise
    out = AuthorOut.model_validate(author)
    await session.commit()
    return out


async def update_author(
    session: AsyncSession, author_id: uuid.UUID, payload: AuthorUpdate
) -> AuthorOut:
    changes = {k: v for k, v in (("name", payload.name), ("email", payload.email)) if v is not None}
    if not changes:
        # Nothing to change: still 404 for an unknown id.
        author = await session.get(Author, author_id)
        if author is None:
            raise _not_found(author_id)
        return AuthorOut.model_validate(author)

    try:
        author = (
            await session.execute(
                update(Author)
                .where(Author.id == author_id)
                .values(**changes)
                .returning(Author)
                .execution_options(synchronize_session=False)
            )
        ).scalar_one_or_none()
    except IntegrityError as exc:
        await session.rollback()
        if _sqlstate(exc) == UNIQUE_VIOLATION:
            raise _duplicate_email(payload.email or "") from exc
        raise
    if author is None:
        raise _not_found(author_id)
    out = AuthorOut.model_validate(author)
    await session.commit()
    return out


async def delete_author(session: AsyncSession, author_id: uuid.UUID) -> None:
    try:
        deleted = (
            await session.execute(delete(Author).where(Author.id == author_id).returning(Author.id))
        ).scalar_one_or_none()
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _sqlstate(exc) == FK_VIOLATION:
            raise ConflictError(
                f"Author with ID '{author_id}' still has messages and cannot be deleted."
            ) from exc
        raise
    if deleted is None:
        raise _not_found(author_id)
