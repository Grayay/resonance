"""Persist each event's publication target; existing events remain main-channel events."""

import sqlalchemy as sa

from alembic import op

revision = "0003_publication_target"
down_revision = "0002_event_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(
            sa.Column(
                "publication_target",
                sa.String(8),
                server_default="main",
                nullable=False,
            )
        )
        batch.create_check_constraint(
            "ck_events_publication_target",
            "publication_target IN ('main', 'test')",
        )


def downgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("ck_events_publication_target", type_="check")
        batch.drop_column("publication_target")
