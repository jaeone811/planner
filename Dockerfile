# 기본 이미지: 텍스트 브리핑 전용 (Chromium 없음 → 이미지 수백 MB, 메모리 512Mi 로 충분)
# 캡처 이미지 브리핑(BRIEF_MODE=image)이 필요하면 Dockerfile.capture 를 사용
# Docker Hub 요청 제한을 피하려고 구글이 운영하는 Docker Hub 미러 사용 (python:3.11-slim 과 동일)
FROM mirror.gcr.io/library/python:3.11-slim

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

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
