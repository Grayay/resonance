import re
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateColumn, CreateIndex

from resonance_bot.db.base import Base


def migration(name):
    path = Path(__file__).parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = spec_from_file_location(name, path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payment_migration_preserves_legacy_data_and_downgrades():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration("0001_initial").upgrade()
            connection.execute(
                sa.text("INSERT INTO users (telegram_user_id, first_name) VALUES (1, 'User')")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO events (id, title, description, starts_at, venue_name, address, "
                    "map_url, capacity, status, reminder_offsets_hours, created_by) VALUES "
                    "(1, 'Legacy', 'Description', '2030-01-01', 'Venue', 'Address', "
                    "'https://example.com', 1, 'REGISTRATION_OPEN', '[]', 1)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO registrations (id, event_id, user_id, status) "
                    "VALUES (1, 1, 1, 'REGISTERED')"
                )
            )
            payments = migration("0002_event_payments")
            payments.upgrade()
            assert connection.execute(sa.text("SELECT is_paid, price FROM events")).one() == (
                0,
                None,
            )
            assert connection.execute(
                sa.text("SELECT payment_status, payment_amount, payment_cycle FROM registrations")
            ).one() == ("NOT_REQUIRED", None, 0)
            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(sa.text("UPDATE events SET is_paid = 1, price = 0"))
            connection.execute(sa.text("UPDATE events SET is_paid = 1, price = 500"))
            connection.execute(
                sa.text(
                    "UPDATE registrations SET payment_status = 'RECEIVED', "
                    "payment_amount = 500, payment_cycle = 1"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO payment_receipts "
                    "(registration_id, payment_cycle, amount, file_id, "
                    "file_unique_id, received_at) "
                    "VALUES (1, 1, 500, 'receipt', 'unique', '2030-01-01')"
                )
            )
            connection.execute(sa.text("UPDATE events SET price = 700"))
            assert connection.scalar(sa.text("SELECT payment_amount FROM registrations")) == 500
            assert connection.scalar(sa.text("SELECT amount FROM payment_receipts")) == 500
            connection.execute(
                sa.text(
                    "UPDATE registrations SET payment_status = 'PENDING', "
                    "payment_amount = 700, payment_cycle = 2"
                )
            )
            assert connection.execute(
                sa.text("SELECT payment_cycle, amount, file_id FROM payment_receipts")
            ).one() == (1, 500, "receipt")
            # PAYMENT fits the existing VARCHAR ReminderKind without altering normal jobs.
            connection.execute(
                sa.text(
                    "INSERT INTO reminders (registration_id, kind, offset_hours, scheduled_at, "
                    "status, attempts) VALUES (1, 'PAYMENT', 0, '2030-01-01', 'SENT', 1)"
                )
            )
            payments.downgrade()
            assert connection.scalar(sa.text("SELECT count(*) FROM registrations")) == 1
            assert connection.scalar(sa.text("SELECT count(*) FROM events")) == 1
            assert "is_paid" not in {
                c["name"] for c in sa.inspect(connection).get_columns("events")
            }
            assert "payment_receipts" not in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_publication_target_migration_preserves_existing_events_and_publications():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration("0001_initial").upgrade()
        migration("0002_event_payments").upgrade()
        connection.execute(
            sa.text(
                "INSERT INTO events (id, title, description, starts_at, venue_name, address, "
                "map_url, capacity, status, reminder_offsets_hours, created_by) VALUES "
                "(1, 'Legacy', 'Description', '2030-01-01', 'Venue', 'Address', "
                "'https://example.com', 1, 'DRAFT', '[]', 1)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO channel_publications (event_id, channel_id, text_message_id) "
                "VALUES (1, '-100123', 42)"
            )
        )
        target = migration("0003_publication_target")
        target.upgrade()
        assert connection.scalar(sa.text("SELECT publication_target FROM events")) == "main"
        connection.execute(sa.text("UPDATE events SET is_paid = 1, price = 500"))
        details = migration("0004_event_payment_details")
        details.upgrade()
        assert connection.execute(
            sa.text("SELECT is_paid, price, payment_details FROM events")
        ).one() == (1, 500, None)
        connection.execute(sa.text("UPDATE events SET payment_details = 'Bank'"))
        details.downgrade()
        assert "payment_details" not in {
            column["name"] for column in sa.inspect(connection).get_columns("events")
        }
        connection.execute(sa.text("UPDATE events SET publication_target = 'test'"))
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text("UPDATE events SET publication_target = 'invalid'"))
        assert connection.execute(
            sa.text("SELECT channel_id, text_message_id FROM channel_publications")
        ).one() == ("-100123", 42)
        target.downgrade()
        assert "publication_target" not in {
            column["name"] for column in sa.inspect(connection).get_columns("events")
        }
        target.upgrade()
        assert connection.scalar(sa.text("SELECT publication_target FROM events")) == "main"
    engine.dispose()


def test_postgresql_migrations_match_model_columns_indexes_and_named_constraints():
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    with Operations.context(context):
        migration("0001_initial").upgrade()
        migration("0002_event_payments").upgrade()
        migration("0003_publication_target").upgrade()
        migration("0004_event_payment_details").upgrade()
    sql = output.getvalue()

    def normalize(value):
        return " ".join(str(value).split())

    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        match = re.search(rf"CREATE TABLE {table.name} \((.*?)\n\);", sql, re.S)
        assert match is not None, table.name
        additions = re.findall(rf"ALTER TABLE {table.name} ADD COLUMN ([^;]+);", sql)
        table_sql = normalize(match[1] + " " + " ".join(additions))
        for column in table.columns:
            expected = normalize(CreateColumn(column).compile(dialect=dialect))
            assert expected in table_sql, f"Missing PostgreSQL definition: {table.name}.{expected}"
        for index in table.indexes:
            assert normalize(CreateIndex(index).compile(dialect=dialect)) in normalize(sql)
        for constraint in table.constraints:
            if constraint.name:
                assert f"CONSTRAINT {constraint.name}" in sql
    assert "ADD COLUMN is_paid BOOLEAN DEFAULT false NOT NULL" in sql
    assert "CREATE TYPE payment_status" not in sql  # Stored enum names use VARCHAR.
