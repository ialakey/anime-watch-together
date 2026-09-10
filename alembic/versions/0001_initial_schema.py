"""Начальная схема: пользователи, список аниме, прогресс по сериям.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=64), nullable=False),
        sa.Column("avatar_url", sa.String(length=255), nullable=True),
        sa.Column("is_admin", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_id", name="uq_users_provider_external_id"),
    )
    op.create_table(
        "episode_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("anime_id", sa.String(length=128), nullable=False),
        sa.Column("episode", sa.Integer(), nullable=False),
        sa.Column("position", sa.Float(), nullable=False),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("translation", sa.String(length=128), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "source", "anime_id", "episode", name="uq_progress_user_anime_episode"
        ),
    )
    with op.batch_alter_table("episode_progress", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_episode_progress_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_progress_user_updated", ["user_id", "updated_at"], unique=False)

    op.create_table(
        "watchlist_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("anime_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("poster_url", sa.String(length=512), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "planned",
                "watching",
                "completed",
                "on_hold",
                "dropped",
                name="watch_status",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("episodes_total", sa.Integer(), nullable=True),
        sa.Column("last_episode", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", "anime_id", name="uq_watchlist_user_anime"),
    )
    with op.batch_alter_table("watchlist_entries", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_watchlist_entries_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_watchlist_entries_user_id"), ["user_id"], unique=False)
        batch_op.create_index("ix_watchlist_user_status", ["user_id", "status"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("watchlist_entries", schema=None) as batch_op:
        batch_op.drop_index("ix_watchlist_user_status")
        batch_op.drop_index(batch_op.f("ix_watchlist_entries_user_id"))
        batch_op.drop_index(batch_op.f("ix_watchlist_entries_status"))

    op.drop_table("watchlist_entries")
    with op.batch_alter_table("episode_progress", schema=None) as batch_op:
        batch_op.drop_index("ix_progress_user_updated")
        batch_op.drop_index(batch_op.f("ix_episode_progress_user_id"))

    op.drop_table("episode_progress")
    op.drop_table("users")
