import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "authors"
    # Server-side defaults are fetched with RETURNING on insert, so no lazy load is needed
    # afterwards (lazy loads don't work on an async session).
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    name: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["Message"]] = relationship(
        back_populates="author",
        lazy="raise",
        order_by="Message.created_at.desc(), Message.id.desc()",
    )


class Message(Base):
    __tablename__ = "messages"
    __mapper_args__ = {"eager_defaults": True}
    # Matches the list endpoint's ORDER BY created_at DESC, id DESC: a stable page order
    # without a full-table sort per request.
    __table_args__ = (Index("ix_messages_created_at_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    title: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(String(1000))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("authors.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Optimistic locking - see update_message in app/services/messages.py.
    version: Mapped[int] = mapped_column(server_default=text("0"))

    author: Mapped[Author] = relationship(back_populates="messages", lazy="raise")
