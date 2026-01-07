# ───────── 베이스 이미지 ─────────
FROM python:3.12
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

RUN apt-get update && apt-get install -y tzdata \
 && ln -fs /usr/share/zoneinfo/Asia/Seoul /etc/localtime \
 && dpkg-reconfigure -f noninteractive tzdata

WORKDIR /app

# 소스 코드
COPY app ./app
COPY requirements.txt .

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
    PORT=8000 \
    ROLE=api   # 기본은 API

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

EXPOSE 8000

# ───────── 엔트리포인트 스크립트 ─────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
