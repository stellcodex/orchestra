FROM python:3.11-slim

WORKDIR /srv

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl netcat-openbsd \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /srv/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /srv/requirements.txt

COPY runtime_app /srv/runtime_app

ENV PYTHONPATH=/srv

CMD ["sh", "-lc", "\
  BACKEND_HOST=${BACKEND_HOST:-backend}; BACKEND_PORT=${BACKEND_PORT:-8000}; \
  STELLAI_HOST=${STELLAI_HOST:-stellai}; STELLAI_PORT=${STELLAI_PORT:-7020}; \
  echo \"Waiting for ${BACKEND_HOST}:${BACKEND_PORT}...\" && until nc -z ${BACKEND_HOST} ${BACKEND_PORT}; do sleep 1; done; \
  echo \"Waiting for ${STELLAI_HOST}:${STELLAI_PORT}...\" && until nc -z ${STELLAI_HOST} ${STELLAI_PORT}; do sleep 1; done; \
  uvicorn runtime_app.main:app --host 0.0.0.0 --port 7010 \
"]
