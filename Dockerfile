FROM python:3.12-slim AS base
WORKDIR /app
COPY sustain ./sustain
ENTRYPOINT ["python", "-m", "sustain"]

# 测试镜像：docker compose --profile test run --rm sustain-test
FROM base AS test
RUN pip install --no-cache-dir pytest
COPY tests ./tests
COPY conftest.py pyproject.toml ./
ENTRYPOINT ["python", "-m", "pytest", "-q"]
