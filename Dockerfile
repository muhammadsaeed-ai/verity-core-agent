FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip && pip install -r requirements.txt
COPY . .
RUN groupadd --system --gid 10001 verity \
    && useradd --system --uid 10001 --gid verity --home-dir /app --no-create-home verity \
    && mkdir -p /app/data /app/outputs \
    && chown -R verity:verity /app/data /app/outputs

USER 10001:10001

EXPOSE 8000
CMD ["python", "main.py", "--serve"]
