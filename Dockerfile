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
# ★ API 키는 **이미지에 굽지 않는다.** ENV 로 박으면 레이어에 평문으로 남아
#   ECR 을 pull 할 수 있는 누구나 `docker image inspect` 로 읽는다
#   (2026-08-12 실측: GOOGLE/ANTHROPIC/OPENAI 3종이 그대로 노출돼 있었다).
#   GitHub Secrets 로 관리하는 의미가 거기서 사라진다.
#   런타임에는 EC2 의 `--env-file /home/ubuntu/app/.env` 로 주입된다
#   (.env 작성은 deploy-ecr.yml 의 "Write .env on EC2" 스텝).
ARG ENV=dev

ENV ENV=${ENV} \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# 소스 코드 — 의존성 변경 없이 코드만 바뀔 때 캐시 활용
COPY app ./app

# ───────── 빌드 게이트: import 스모크 ─────────
# ★ 여기서 실패하면 이미지가 ECR 로 나가지 못한다.
#   2026-08-12: 전이 의존 mcp 가 2.0 으로 올라와 `app.main` import 가 깨졌는데,
#   그대로 push·배포돼 dev 가 이틀간 502(크래시 루프 2619회)였다.
#   깨진 이미지는 **애초에 만들어지지 않게** 한다 — 배포 후 롤백보다 싸다.
# ★ import 시점에 SQLAlchemy 가 DB URL 을 조립하고 LLM 클라이언트가 키를 읽으므로
#   **더미 값**을 준다. 접속·호출은 하지 않는다(engine 은 lazy).
#   실제 값은 런타임 --env-file 로만 온다.
RUN MS_DB_HOST=localhost MS_DB_PORT=1433 MS_DB_USER=build MS_DB_PASSWORD=build \
    MS_DB_NAME=build EUN_DB_NAME=build SECRET_KEY=build ALGORITHM=HS256 \
    DB_HOST=localhost DB_PORT=3306 DB_USER=build DB_PASSWORD=build DB_NAME=build \
    GOOGLE_API_KEY=build ANTHROPIC_API_KEY=build OPENAI_API_KEY=build \
    python -c "import app.main" \
 && echo "[build-gate] app.main import OK"

EXPOSE 8000

# ───────── 엔트리포인트 스크립트 ─────────
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
