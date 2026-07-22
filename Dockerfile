FROM python:3.13-slim

WORKDIR /srv/app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# SQLite lives on a volume so the slug registry survives redeploys.
ENV DB_PATH=/data/slugs.db
VOLUME /data

RUN useradd --system --no-create-home appuser && mkdir -p /data && chown appuser /data
USER appuser

EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
