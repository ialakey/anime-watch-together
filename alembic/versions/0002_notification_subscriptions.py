"""Подписки на новые серии и настройки уведомлений у пользователя.

Revision ID: 0002_notifications
Revises: 0001_initial
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_notifications"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        # sa.true(), а не text("1"): postgres не принимает целое как default
        # для boolean, а sqlite — наоборот, не знает литерала true
        batch_op.add_column(
            sa.Column(
                "notifications_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(sa.Column("notify_channel_id", sa.String(length=32), nullable=True))

    op.create_table(
        "notification_subscriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("anime_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("poster_url", sa.String(length=512), nullable=True),
        sa.Column("last_known_episode", sa.Integer(), nullable=False),
        sa.Column("last_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "source", "anime_id", name="uq_subscription_user_anime"),
    )
    with op.batch_alter_table("notification_subscriptions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_notification_subscriptions_user_id"), ["user_id"], unique=False
        )
        batch_op.create_index("ix_subscription_anime", ["source", "anime_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("notification_subscriptions", schema=None) as batch_op:
        batch_op.drop_index("ix_subscription_anime")
        batch_op.drop_index(batch_op.f("ix_notification_subscriptions_user_id"))

    op.drop_table("notification_subscriptions")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("notify_channel_id")
        batch_op.drop_column("notifications_enabled")
