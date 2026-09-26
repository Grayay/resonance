FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY resonance_bot ./resonance_bot
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir .

CMD ["python", "-m", "resonance_bot"]

