"""Store payment details on each event."""

import sqlalchemy as sa

from alembic import op

revision = "0004_event_payment_details"
down_revision = "0003_publication_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("events", sa.Column("payment_details", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("events", "payment_details")
