FROM python:3.12-slim
WORKDIR /app
COPY gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway ./gateway
ENV PORT=8080
CMD ["sh", "-c", "uvicorn gateway.app:create_app --factory --host 0.0.0.0 --port ${PORT} --no-access-log --proxy-headers --forwarded-allow-ips='*'"]
