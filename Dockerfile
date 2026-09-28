FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir flask==3.0.3 docker==7.1.0 httpx==0.27.2

COPY app.py .
COPY static/ static/

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/api/health',timeout=5)"

CMD ["python", "app.py"]
