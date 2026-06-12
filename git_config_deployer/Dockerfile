ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 git openssh-client

COPY app /app

CMD ["python3", "-u", "/app/server.py"]
