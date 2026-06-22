FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN python -m pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY index.html ./index.html
COPY privacy-policy.html ./privacy-policy.html
COPY terms-of-service.html ./terms-of-service.html

RUN mkdir -p /app/backend/uploads /app/backend/data

WORKDIR /app/backend
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
