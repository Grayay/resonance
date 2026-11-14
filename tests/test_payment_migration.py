from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


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
