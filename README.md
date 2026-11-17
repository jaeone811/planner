# Daily Planner + 카톡 자동 캡처 브리핑

다크모드 모바일 일일 플래너(10분 단위 타임테이블) 웹앱과, 지정한 시각마다 플래너 화면을 캡처해서
카카오톡 **나와의 채팅**과 **ntfy 푸시**로 보내 주는 FastAPI 백엔드입니다. Google Cloud Run에 배포하는 것을 기준으로 만들었습니다.

```
planner/
├── main.py              # FastAPI: 일정/설정 API, 크론, Playwright 캡처, Pillow 상태바, 카카오·ntfy 발송
├── static/
│   ├── index.html       # 단일 파일 HTML/CSS/Vanilla JS 플래너 UI
│   └── fonts/           # Pretendard (SIL OFL 1.1, OFL-LICENSE.txt 포함)
├── Dockerfile           # mcr.microsoft.com/playwright/python:v1.65.2-noble, 포트 8080
├── requirements.txt
├── .env.example
└── .dockerignore / .gitignore
```

저장소 레이아웃(GCS 버킷, 또는 로컬 `./data`)

| 파일 | 내용 |
|---|---|
| `plans/YYYY-MM-DD.json` | `{date, tasks:[{id,name,color,todos:[{id,text,done}]}], timeline:{"슬롯":"taskId"}, updated_at}` — 슬롯 0 = 05:00~05:10, 143 = 04:50~05:00 |
| `settings.json` | `{notify_times:["08:00","18:30"], nag_enabled, nag_interval_minutes}` |
| `kakao_token.json` | access/refresh 토큰과 만료 시각. 갱신할 때마다 자동으로 덮어씁니다 |
| `state.json` | 날짜별 발송 기록(중복 발송 방지), 마지막 독촉 시각, 마지막 크론 시각 |

---

## 1. 화면 사용법

- **카테고리 선택 → 오른쪽 칸 터치/드래그**: 선택한 색으로 칠해지고, 카테고리 시간(`1:30:00`)과 우상단 합계(`1H 30M`)가 바로 다시 계산됩니다. 이미 그 색으로 칠해진 칸에서 드래그를 시작하면 지우기 모드로 동작합니다.
- **`+ To-do`**: [할 일] 탭에서는 카테고리를 고르고 Enter를 눌러 할 일을 연속으로 추가합니다. [카테고리] 탭에서는 이름과 색을 정해 새로 만듭니다.
- **수정·삭제**: ✏️ 편집 모드를 켜고 카테고리나 할 일을 탭하거나, 아무 때나 **길게 누르면** 편집 시트가 열립니다.
- **날짜 이동**: 날짜 양옆 `‹ ›`, 날짜를 탭해서 달력 선택, 헤더 좌우 스와이프, 하단 `오늘` 버튼.
- **👁 미리보기**: 카톡으로 나갈 캡처 화면을 미리 봅니다.
- **🔔 알림 설정**: 브리핑 시각(여러 개 추가 가능), 미작성 독촉 알림 켜기/끄기와 간격, 카카오 연결, 캡처 이미지 보기, 테스트 발송.
- **✓ 저장**: 변경 사항은 1.2초 뒤에 자동 저장되고, 버튼을 누르면 바로 저장됩니다. 저장되지 않은 변경이 있으면 주황색 점이 표시됩니다.
- `?capture=1`을 붙이면 툴바와 편집 버튼을 숨기고, 상단에 상태바를 합성할 52px 여백을 둡니다.

## 2. 크론 동작 (`POST /api/cron`, 헤더 `X-App-Key`)

한국 시각(`Asia/Seoul`)을 기준으로 다음 순서로 판단합니다.

- **A. 미작성 독촉**: 오늘 `tasks`가 비어 있고, 독촉이 켜져 있고, `08:00 ≤ 지금 ≤ 22:00`일 때 발송합니다.
  - 카톡 text 템플릿과 "플래너 작성하기" 버튼을 보내고 ntfy 푸시도 함께 보냅니다.
  - 매분 호출되므로 `nag_interval_minutes`(기본 2시간) 간격으로만 보냅니다.
- **B. 완성본 브리핑**: 오늘 플래너가 있고 지금 시각이 `notify_times`에 해당하면 아래 순서로 보냅니다.
  1. Playwright로 `390×790 @2x`를 캡처합니다.
  2. Pillow로 iPhone 상태바(시각, 안테나, LTE, 배터리)를 합성합니다.
     - 시각과 배터리는 기본적으로 캡처할 때마다 랜덤입니다. 범위는 `STATUS_TIME_RANGE`, `STATUS_BATTERY_RANGE`로 정합니다.
     - `STATUS_TIME_MODE=real`이면 실제 발송 시각과 고정 배터리(`STATUS_BATTERY_FIXED`)를 씁니다.
  3. 카카오에 이미지를 업로드하고 Feed 템플릿으로 나에게 보냅니다.
  4. ntfy로 `Click: kakaotalk://`, `Priority: high` 푸시를 보냅니다.
  - 스케줄러가 몇 분 늦게 불러도 놓치지 않도록 `NOTIFY_CATCHUP_MINUTES`(기본 9분) 안의 지연은 허용합니다. 같은 시각은 `state.json`에 기록해 한 번만 보냅니다.
- 카톡 발송이 실패하면(예: 토큰 만료) ntfy로 "⚠️ 발송 실패" 사유를 대신 보냅니다.

수동 확인용 API(모두 key 필요):

- `GET /api/preview.png?date=YYYY-MM-DD`: 합성이 끝난 이미지
- `POST /api/notify/test {"kind":"brief"|"nag","date":"..."}`: 조건과 상관없이 즉시 발송
- `GET /api/status`: 카카오, ntfy, 저장소 상태

## 3. 로컬 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # Docker에서는 불필요
cp .env.example .env                 # APP_SECRET_KEY 등을 채움 (GCS_BUCKET을 비우면 ./data에 저장)
uvicorn main:app --port 8080
# 브라우저: http://localhost:8080/?key=<APP_SECRET_KEY>
```

Docker로 실행할 때:

```bash
docker build -t planner .
docker run --rm -p 8080:8080 --env-file .env planner
```

## 4. 카카오 설정 (나에게 보내기)

1. [Kakao Developers](https://developers.kakao.com)에서 애플리케이션을 만들고 **REST API 키**를 `KAKAO_REST_API_KEY`에 넣습니다.
2. **카카오 로그인을 활성화**하고 Redirect URI에 `https://<Cloud Run URL>/kakao/callback`을 등록합니다. 로컬에서 테스트하려면 `http://localhost:8080/kakao/callback`도 등록합니다.
3. **동의항목**에서 **카카오톡 메시지 전송(`talk_message`)**을 켭니다.
4. **플랫폼 > Web 사이트 도메인**에 Cloud Run URL을 등록합니다. 메시지 버튼 링크는 등록된 도메인이어야 열립니다.
5. 클라이언트 시크릿을 켜 두었다면 `KAKAO_CLIENT_SECRET`도 넣습니다.
6. 배포한 뒤 플래너의 🔔 → **카카오 연결**을 누르거나 `https://<URL>/kakao/login?key=<APP_SECRET_KEY>`로 접속합니다. 동의를 마치면 `kakao_token.json`이 저장됩니다.
7. 토큰 수명:
   - access token은 자동으로 갱신됩니다.
   - refresh token은 남은 유효기간이 1개월 미만일 때 갱신 응답에 새 값이 함께 오고, 이 값은 자동으로 GCS에 덮어씁니다.
   - 오랫동안 쓰지 않아 refresh token까지 만료되면(KOE322) 6번을 다시 하면 됩니다.

> 카카오 콘솔 메뉴 이름은 개편될 수 있습니다. 위 항목(Redirect URI, talk_message 동의항목, Web 도메인)을 기준으로 찾으세요.

## 5. ntfy (아이폰 잠금화면 알림)

1. App Store에서 **ntfy**를 설치하고, 추측하기 어려운 이름의 토픽(예: `planner-8f3k2-x9q`)을 구독합니다.
2. 그 이름을 `NTFY_TOPIC`에 넣습니다. ntfy.sh 공개 서버에서는 토픽 이름이 곧 비밀번호 역할을 합니다.
3. 알림을 탭하면 `kakaotalk://`로 카카오톡이 열립니다.

---

## 6. Google Cloud 배포 (gcloud CLI)

```bash
# ── 0) 변수 ──────────────────────────────────────────────────────────
PROJECT_ID="my-gcp-project"
REGION="asia-northeast3"                  # 서울
SERVICE="planner"
BUCKET="${PROJECT_ID}-planner-data"
SA_NAME="planner-run"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
APP_SECRET_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"   # 콤마 없는 값이어야 함
echo "APP_SECRET_KEY=$APP_SECRET_KEY"      # 따로 안전하게 보관

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

# ── 1) GCS 버킷 (비공개) ──────────────────────────────────────────────
gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention

# ── 2) Cloud Run 전용 서비스 계정 + 버킷 권한 ─────────────────────────
gcloud iam service-accounts create "$SA_NAME" --display-name="Planner Cloud Run"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA}" --role="roles/storage.objectAdmin"

# ── 3) 환경변수 파일 (git에 올리지 말 것: .gitignore에 env.yaml 포함) ─
cat > env.yaml <<EOF
APP_SECRET_KEY: "${APP_SECRET_KEY}"
GCS_BUCKET: "${BUCKET}"
PUBLIC_BASE_URL: "https://placeholder.invalid"
KAKAO_REST_API_KEY: "카카오_REST_API_키"
KAKAO_CLIENT_SECRET: ""
NTFY_TOPIC: "planner-추측어려운-토픽"
EOF

# ── 4) 배포 (Dockerfile 기반 소스 빌드) ───────────────────────────────
gcloud run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$SA" --allow-unauthenticated \
  --memory 2Gi --cpu 2 --timeout 300 --concurrency 20 \
  --min-instances 0 --max-instances 1 \
  --env-vars-file env.yaml

# ── 5) 서비스 URL을 PUBLIC_BASE_URL로 반영 ────────────────────────────
URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
gcloud run services update "$SERVICE" --region "$REGION" --update-env-vars "PUBLIC_BASE_URL=${URL}"
echo "플래너: ${URL}/?key=${APP_SECRET_KEY}"
echo "카카오 Redirect URI 등록: ${URL}/kakao/callback"

# ── 6) Cloud Scheduler: 매분 크론 호출 ────────────────────────────────
gcloud scheduler jobs create http planner-cron \
  --location="$REGION" --schedule="* * * * *" --time-zone="Asia/Seoul" \
  --uri="${URL}/api/cron" --http-method=POST \
  --headers="X-App-Key=${APP_SECRET_KEY},Content-Type=application/json" \
  --message-body='{}' --attempt-deadline=180s

# 즉시 한 번 실행해서 확인 / 로그 보기
gcloud scheduler jobs run planner-cron --location="$REGION"
gcloud run services logs read "$SERVICE" --region "$REGION" --limit 50
```

### 운영 메모

- **스케줄 주기**: 브리핑 시각을 분 단위로 정확히 맞추려고 `* * * * *`(매분)으로 둡니다.
  - 요청 대부분은 수 ms 안에 끝나고, Cloud Scheduler 작업 1개는 무료 할당량(계정당 3개) 안에 들어갑니다.
  - 비용을 더 줄이려면 `*/5 * * * *`로 바꾸고 알림 시각을 5분 단위로 설정하세요. `NOTIFY_CATCHUP_MINUTES` 덕분에 지연돼도 놓치지 않습니다.
- **메모리**: Chromium 캡처에 1GB 이상이 필요해서 `2Gi`로 잡았습니다. `--max-instances 1`이면 발송 기록(`state.json`) 경합이 생기지 않습니다.
- **보안**:
  - 웹앱, API, 크론 모두 `APP_SECRET_KEY`로 보호됩니다. 페이지는 `Referrer-Policy: no-referrer`와 `noindex`를 적용합니다.
  - 카톡 버튼 링크에는 바로 열 수 있도록 `?key=`가 들어갑니다. 원하지 않으면 `KAKAO_LINK_INCLUDE_KEY=false`로 바꿉니다.
  - ntfy 푸시에는 key를 넣지 않습니다.
- **하루 기준**: 기본은 자정입니다. `DAY_START_HOUR=5`로 두면 그리드(05시~익일 04시)와 똑같이 새벽 0~4시를 전날 플래너로 취급합니다.
- **Playwright 버전**: `:latest` 태그는 더 이상 배포되지 않으므로 이미지 태그와 `requirements.txt`의 `playwright==` 버전을 함께 올려야 합니다.
  - 이 이미지는 Ubuntu 24.04 기반이라 **Python 3.12**가 들어 있습니다. 코드는 3.11 이상이면 동작합니다.
