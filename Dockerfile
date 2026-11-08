FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 tp4by \
    && mkdir /app/data && chown tp4by:tp4by /app/data
USER tp4by
VOLUME ["/app/data"]
CMD ["tp4by"]
