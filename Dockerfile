# Playwright 공식 Python 이미지 (Ubuntu 24.04 noble, Chromium·의존성 내장)
# 주의: :latest 태그는 더 이상 배포되지 않으므로 버전을 고정한다. requirements.txt 의 playwright 버전과 일치해야 함.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .
COPY static ./static

EXPOSE 8080

# Cloud Run 이 주입하는 $PORT 사용. 크론 요청 처리 중 Playwright 가 같은 프로세스에 다시 접속하므로 단일 워커 + async 로 충분.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
