# Le tag de l'image doit correspondre à la version de playwright dans requirements.txt
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

ENV PIP_BREAK_SYSTEM_PACKAGES=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}
