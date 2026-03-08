FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn httpx pyyaml

COPY litellm_gateway.py /app/litellm_gateway.py

CMD ["uvicorn", "litellm_gateway:app", "--host", "0.0.0.0", "--port", "4000"]
