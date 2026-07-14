FROM python:3.12-alpine

WORKDIR /app

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN HTTP_PROXY="$HTTP_PROXY" \
    HTTPS_PROXY="$HTTPS_PROXY" \
    NO_PROXY="$NO_PROXY" \
    http_proxy="$HTTP_PROXY" \
    https_proxy="$HTTPS_PROXY" \
    no_proxy="$NO_PROXY" \
    pip install \
      --no-cache-dir \
      --prefer-binary \
      --default-timeout=120 \
      --retries=5 \
      -r requirements.txt

COPY app ./app

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
