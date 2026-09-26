"""initial schema: authors, messages

Revision ID: 0001
Revises:
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # postgres_exporter's --collector.stat_statements and the "PostgreSQL Ops & Queries" Grafana
    # dashboard need it (k8s/postgres-statefulset.yaml preloads the library).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")

    op.create_table(
        "authors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("email", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="authors_pkey"),
        sa.UniqueConstraint("email", name="authors_email_key"),
    )
    op.create_table(
        "messages",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=100), nullable=False),
        sa.Column("content", sa.String(length=1000), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Optimistic locking: UPDATE ... WHERE id = :id AND version = :version.
        sa.Column("version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"], ["authors.id"], name="messages_author_id_fkey", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="messages_pkey"),
    )
    op.create_index("ix_messages_created_at_id", "messages", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_messages_created_at_id", table_name="messages")
    op.drop_table("messages")
    op.drop_table("authors")
