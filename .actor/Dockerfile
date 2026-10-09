FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /usr/src/app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install . apify

CMD ["python", "-m", "agent.actor"]
