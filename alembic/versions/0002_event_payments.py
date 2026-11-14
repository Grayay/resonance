"""Add per-event prices and per-registration payments; existing data stays free.

Revision ID: 0002_event_payments
Revises: 0001_initial
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_event_payments"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("events") as batch:
        batch.add_column(
            sa.Column("is_paid", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
        batch.add_column(sa.Column("price", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_events_payment_price",
            "(is_paid AND price IS NOT NULL AND price > 0) OR (NOT is_paid AND price IS NULL)",
        )
    with op.batch_alter_table("registrations") as batch:
        batch.add_column(
            sa.Column(
                "payment_status",
                sa.Enum(
                    "NOT_REQUIRED", "PENDING", "RECEIVED", name="payment_status", native_enum=False
                ),
                server_default="NOT_REQUIRED",
                nullable=False,
            )
        )
        batch.add_column(sa.Column("payment_amount", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("payment_cycle", sa.Integer(), server_default="0", nullable=False)
        )
        batch.create_check_constraint(
            "ck_registration_payment",
            "(payment_status = 'NOT_REQUIRED' AND payment_amount IS NULL AND payment_cycle = 0) OR "
            "(payment_status IN ('PENDING', 'RECEIVED') AND payment_amount IS NOT NULL "
            "AND payment_amount > 0 AND payment_cycle > 0)",
        )
        batch.create_index("ix_registrations_payment_status", ["payment_status"])
    op.create_table(
        "payment_receipts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "registration_id",
            sa.Integer(),
            sa.ForeignKey("registrations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("payment_cycle", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.String(512), nullable=False),
        sa.Column("file_unique_id", sa.String(255), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("registration_id", "payment_cycle", name="uq_receipt_payment_cycle"),
        sa.CheckConstraint("amount > 0 AND payment_cycle > 0", name="ck_receipt_payment"),
    )
    # ReminderKind is a VARCHAR, not a native PostgreSQL enum.


def downgrade() -> None:
    op.execute("DELETE FROM reminders WHERE kind = 'PAYMENT'")
    op.drop_table("payment_receipts")
    with op.batch_alter_table("registrations") as batch:
        batch.drop_index("ix_registrations_payment_status")
        batch.drop_constraint("ck_registration_payment", type_="check")
        batch.drop_column("payment_amount")
        batch.drop_column("payment_cycle")
        batch.drop_column("payment_status")
    with op.batch_alter_table("events") as batch:
        batch.drop_constraint("ck_events_payment_price", type_="check")
        batch.drop_column("price")
        batch.drop_column("is_paid")
