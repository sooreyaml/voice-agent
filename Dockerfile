FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --requirement requirements.txt

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY scripts ./scripts
COPY businesses ./businesses

RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /srv
USER agent

# $PORT is assigned by the host at run time, hence the shell form.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
