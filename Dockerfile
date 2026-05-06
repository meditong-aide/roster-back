# ───────── Stage 1: Build (의존성 설치) ─────────
FROM python:3.12-slim AS builder

# 빌드에 필요한 도구만 일시 설치 (wheel 빌드 시 사용)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements.txt .

# 사용자 디렉터리(root home)에 설치 → Stage 2 로 복사
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir --user -r requirements.txt


# ───────── Stage 2: Runtime ─────────
FROM python:3.12-slim

# 런타임에 필요한 시스템 패키지 (tzdata 만)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
 && ln -fs /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
 && dpkg-reconfigure -f noninteractive tzdata \
 && apt-get clean && rm -rf /var/lib/apt/lists/*

# 빌드 단계에서 설치된 패키지를 runtime 으로 복사
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH

# uv binary (선택) — 기존 동작 유지
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

# ───────── 빌드 인자 ─────────
ARG ENV=dev
ARG GOOGLE_API_KEY
ARG ANTHROPIC_API_KEY
ARG OPENAI_API_KEY

ENV ENV=${ENV} \
    GOOGLE_API_KEY=${GOOGLE_API_KEY} \
    ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY} \
    OPENAI_API_KEY=${OPENAI_API_KEY} \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# 소스 코드 — 의존성 변경 없이 코드만 바뀔 때 캐시 활용
COPY app ./app

EXPOSE 8000

# ───────── 엔트리포인트 스크립트 ─────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
