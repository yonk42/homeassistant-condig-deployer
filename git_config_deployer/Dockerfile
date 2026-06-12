ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-yaml git openssh-client openssh-keygen

COPY app /app

CMD ["python3", "-u", "/app/server.py"]
