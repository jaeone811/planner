"""
Personal Daily Planner + 자동 캡처 브리핑 백엔드

- FastAPI 로 단일 페이지 플래너(static/index.html) 서빙
- 일정/설정/카카오 토큰을 Google Cloud Storage(JSON) 에 저장 (로컬은 ./data 폴백)
- Cloud Scheduler 가 POST /api/cron 을 주기적으로 호출하면
    A) 오늘 플래너가 비어 있으면 → 카카오톡 '나와의 채팅' 독촉 + ntfy 푸시
    B) 오늘 플래너가 있고 지정 시각이면 → Playwright 캡처 + Pillow 상태바 합성
       → 카카오 이미지 업로드 + 나에게 보내기(Feed) → ntfy 푸시(Click: kakaotalk://)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field, field_validator

load_dotenv()

# ──────────────────────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY", "")
PORT = int(os.getenv("PORT", "8080"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
# Playwright 가 같은 컨테이너 안에서 접속할 주소 (Cloud Run 에서도 localhost 로 충분)
INTERNAL_BASE_URL = os.getenv("INTERNAL_BASE_URL", f"http://localhost:{PORT}").rstrip("/")

GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
LOCAL_DATA_DIR = Path(os.getenv("LOCAL_DATA_DIR", str(BASE_DIR / "data")))

KAKAO_REST_API_KEY = os.getenv("KAKAO_REST_API_KEY", "").strip()
KAKAO_CLIENT_SECRET = os.getenv("KAKAO_CLIENT_SECRET", "").strip()
KAKAO_REDIRECT_URI = os.getenv("KAKAO_REDIRECT_URI", f"{PUBLIC_BASE_URL}/kakao/callback").strip()
# 카카오 메시지 버튼 링크에 ?key= 를 포함할지 (나와의 채팅에서 바로 열기 편함)
KAKAO_LINK_INCLUDE_KEY = os.getenv("KAKAO_LINK_INCLUDE_KEY", "true").lower() == "true"

NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_TOKEN = os.getenv("NTFY_TOKEN", "").strip()  # 보호된 토픽/셀프호스팅용 (선택)

TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Seoul"))
NAG_START = os.getenv("NAG_START", "08:00")
NAG_END = os.getenv("NAG_END", "22:00")
# 스케줄러가 몇 분 늦게 호출돼도 해당 알림 시각을 놓치지 않도록 허용하는 지연(분)
NOTIFY_CATCHUP_MINUTES = int(os.getenv("NOTIFY_CATCHUP_MINUTES", "9"))
# 플래너 하루의 시작 시각. 0 이면 자정 기준(요구사항 기본값), 5 로 두면 그리드(05~04시)와 동일하게
# 새벽 0~4시는 '전날' 플래너로 취급한다.
DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "0"))

CAPTURE_WIDTH = 390
CAPTURE_HEIGHT = 790
CAPTURE_SCALE = 2
STATUS_BAR_HEIGHT_PX = 104  # 2x 기준 (CSS 52px)
STATUS_BAR_BG = (0x12, 0x12, 0x12)
STATUS_FONT_PATH = os.getenv("STATUS_FONT_PATH", str(STATIC_DIR / "fonts" / "Pretendard-SemiBold.otf"))
# 캡처 상태바의 시각·배터리: random(기본) = 캡처마다 랜덤 / real = 실제 발송 시각 + 고정 배터리
STATUS_TIME_MODE = os.getenv("STATUS_TIME_MODE", "random").lower()
STATUS_TIME_RANGE = os.getenv("STATUS_TIME_RANGE", "00:00-23:59")  # 랜덤 시각 범위 (HH:MM-HH:MM)
STATUS_BATTERY_RANGE = os.getenv("STATUS_BATTERY_RANGE", "20-100")  # 랜덤 배터리 범위 (%)
STATUS_BATTERY_FIXED = int(os.getenv("STATUS_BATTERY_FIXED", "89"))  # real 모드일 때 배터리
CHROMIUM_EXECUTABLE = os.getenv("CHROMIUM_EXECUTABLE", "").strip() or None  # 로컬 테스트용(선택)

KAKAO_AUTH_HOST = "https://kauth.kakao.com"
KAKAO_API_HOST = "https://kapi.kakao.com"

TOKEN_FILE = "kakao_token.json"
SETTINGS_FILE = "settings.json"
STATE_FILE = "state.json"

SLOTS_PER_DAY = 144  # 24시간 × 6칸(10분)
SLOT_MINUTES = 10
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
WEEKDAYS_KO = "월화수목금토일"

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("planner")


# ──────────────────────────────────────────────────────────────────────────────
# 저장소 (GCS ↔ 로컬 파일 폴백)
# ──────────────────────────────────────────────────────────────────────────────
class Storage:
    def __init__(self) -> None:
        self.bucket = None
        if GCS_BUCKET:
            from google.cloud import storage as gcs  # 지연 import (로컬 테스트 가볍게)

            self.bucket = gcs.Client().bucket(GCS_BUCKET)
            log.info("Storage: GCS bucket gs://%s", GCS_BUCKET)
        else:
            LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
            log.info("Storage: local directory %s", LOCAL_DATA_DIR)

    @property
    def mode(self) -> str:
        return "gcs" if self.bucket is not None else "local"

    def _read(self, name: str) -> dict | None:
        if self.bucket is not None:
            from google.api_core.exceptions import NotFound

            try:
                return json.loads(self.bucket.blob(name).download_as_text(encoding="utf-8"))
            except NotFound:
                return None
        path = LOCAL_DATA_DIR / name
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write(self, name: str, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        if self.bucket is not None:
            blob = self.bucket.blob(name)
            blob.cache_control = "no-store"
            blob.upload_from_string(payload, content_type="application/json; charset=utf-8")
            return
        path = LOCAL_DATA_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    async def read(self, name: str) -> dict | None:
        return await asyncio.to_thread(self._read, name)

    async def write(self, name: str, data: dict) -> None:
        await asyncio.to_thread(self._write, name, data)


storage: Storage  # lifespan 에서 초기화


# ──────────────────────────────────────────────────────────────────────────────
# 데이터 모델
# ──────────────────────────────────────────────────────────────────────────────
class Todo(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    text: str = Field(max_length=200)
    done: bool = False


class Task(BaseModel):
    """카테고리(좌측 컬러 바) + 하위 할 일 목록"""

    id: str = Field(min_length=1, max_length=40)
    name: str = Field(max_length=60)
    color: str = "#6A3CC4"
    todos: list[Todo] = Field(default_factory=list, max_length=100)

    @field_validator("color")
    @classmethod
    def _color(cls, v: str) -> str:
        if not COLOR_RE.match(v):
            raise ValueError("color must be #RRGGBB")
        return v.upper()


class Plan(BaseModel):
    date: str
    tasks: list[Task] = Field(default_factory=list, max_length=50)
    # 슬롯 인덱스("0"~"143", 0 = 05:00~05:10) → task id
    timeline: dict[str, str] = Field(default_factory=dict)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        return validate_date(v)


class Settings(BaseModel):
    notify_times: list[str] = Field(default_factory=lambda: ["22:00"])
    nag_enabled: bool = True
    nag_interval_minutes: int = Field(default=120, ge=30, le=720)

    @field_validator("notify_times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not TIME_RE.match(t):
                raise ValueError(f"invalid time: {t} (HH:MM)")
        return sorted(set(v))[:24]


def validate_date(v: str) -> str:
    if not DATE_RE.match(v):
        raise ValueError("date must be YYYY-MM-DD")
    date.fromisoformat(v)
    return v


def plan_file(d: str) -> str:
    return f"plans/{d}.json"


def empty_plan(d: str) -> dict:
    return {"date": d, "tasks": [], "timeline": {}, "exists": False}


def sanitize_plan(plan: Plan) -> dict:
    task_ids = {t.id for t in plan.tasks}
    timeline: dict[str, str] = {}
    for k, v in plan.timeline.items():
        if k.isdigit() and 0 <= int(k) < SLOTS_PER_DAY and v in task_ids:
            timeline[str(int(k))] = v
    data = plan.model_dump()
    data["timeline"] = dict(sorted(timeline.items(), key=lambda kv: int(kv[0])))
    data["updated_at"] = now_kst().isoformat(timespec="seconds")
    return data


async def load_plan(d: str) -> dict:
    data = await storage.read(plan_file(d))
    if not data:
        return empty_plan(d)
    data["exists"] = True
    return data


async def load_settings() -> Settings:
    data = await storage.read(SETTINGS_FILE)
    return Settings(**data) if data else Settings()


# ──────────────────────────────────────────────────────────────────────────────
# 시간 유틸
# ──────────────────────────────────────────────────────────────────────────────
def now_kst() -> datetime:
    return datetime.now(TZ)


def planner_today(now: datetime | None = None) -> str:
    now = now or now_kst()
    return (now - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def hhmm_to_min(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def date_label(d: str) -> str:
    dt = date.fromisoformat(d)
    return f"{dt.year}. {dt.month}. {dt.day} ({WEEKDAYS_KO[dt.weekday()]})"


def fmt_hm(minutes: int) -> str:
    return f"{minutes // 60}H {minutes % 60}M"


def fmt_hms(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}:00"


def plan_stats(plan: dict) -> dict:
    counts: dict[str, int] = {}
    for tid in plan.get("timeline", {}).values():
        counts[tid] = counts.get(tid, 0) + 1
    tasks = plan.get("tasks", [])
    todos = [td for t in tasks for td in t.get("todos", [])]
    return {
        "total_minutes": sum(counts.values()) * SLOT_MINUTES,
        "per_task": [(t["name"], counts.get(t["id"], 0) * SLOT_MINUTES) for t in tasks],
        "todo_total": len(todos),
        "todo_done": sum(1 for td in todos if td.get("done")),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 인증
# ──────────────────────────────────────────────────────────────────────────────
def key_ok(provided: str | None) -> bool:
    return bool(APP_SECRET_KEY) and bool(provided) and hmac.compare_digest(provided, APP_SECRET_KEY)


def require_key(request: Request) -> None:
    provided = request.headers.get("x-app-key") or request.query_params.get("key")
    if not key_ok(provided):
        raise HTTPException(status_code=401, detail="invalid key")


def app_url(d: str | None = None, with_key: bool = True) -> str:
    params: dict[str, str] = {}
    if with_key:
        params["key"] = APP_SECRET_KEY
    if d:
        params["date"] = d
    return f"{PUBLIC_BASE_URL}/?{urlencode(params)}" if params else f"{PUBLIC_BASE_URL}/"


def sign_state(ts: int) -> str:
    mac = hmac.new(APP_SECRET_KEY.encode(), f"kakao-oauth:{ts}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{mac}"


def verify_state(state: str) -> bool:
    try:
        ts_s, _ = state.split(".", 1)
        ts = int(ts_s)
    except ValueError:
        return False
    return abs(time.time() - ts) < 600 and hmac.compare_digest(sign_state(ts), state)


# ──────────────────────────────────────────────────────────────────────────────
# 카카오 API
# ──────────────────────────────────────────────────────────────────────────────
class KakaoError(RuntimeError):
    pass


def _kakao_token_body(extra: dict) -> dict:
    body = {"client_id": KAKAO_REST_API_KEY, **extra}
    if KAKAO_CLIENT_SECRET:
        body["client_secret"] = KAKAO_CLIENT_SECRET
    return body


def _token_record(resp: dict, prev: dict | None = None) -> dict:
    now = int(time.time())
    rec = dict(prev or {})
    rec["access_token"] = resp["access_token"]
    rec["token_type"] = resp.get("token_type", "bearer")
    rec["expires_at"] = now + int(resp.get("expires_in", 0))
    # 갱신 응답에는 refresh_token 이 '남은 유효기간 1개월 미만'일 때만 새로 포함된다.
    if resp.get("refresh_token"):
        rec["refresh_token"] = resp["refresh_token"]
        rec["refresh_token_expires_at"] = now + int(resp.get("refresh_token_expires_in", 0))
    if resp.get("scope"):
        rec["scope"] = resp["scope"]
    rec["updated_at"] = now_kst().isoformat(timespec="seconds")
    return rec


async def kakao_exchange_code(client: httpx.AsyncClient, code: str) -> dict:
    r = await client.post(
        f"{KAKAO_AUTH_HOST}/oauth/token",
        data=_kakao_token_body({"grant_type": "authorization_code", "redirect_uri": KAKAO_REDIRECT_URI, "code": code}),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    if r.status_code != 200:
        raise KakaoError(f"token exchange failed: {r.status_code} {r.text}")
    rec = _token_record(r.json())
    await storage.write(TOKEN_FILE, rec)
    return rec


async def kakao_access_token(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    tok = await storage.read(TOKEN_FILE)
    if not tok or not tok.get("refresh_token"):
        raise KakaoError("카카오 계정이 연결되지 않았습니다. /kakao/login 으로 먼저 연결하세요.")
    if not force_refresh and tok.get("expires_at", 0) - time.time() > 300:
        return tok["access_token"]
    if not KAKAO_REST_API_KEY:
        raise KakaoError("KAKAO_REST_API_KEY 가 설정되지 않았습니다.")

    r = await client.post(
        f"{KAKAO_AUTH_HOST}/oauth/token",
        data=_kakao_token_body({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]}),
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    if r.status_code != 200:
        # KOE322 등: refresh_token 만료 → 재로그인 필요
        raise KakaoError(f"토큰 갱신 실패({r.status_code}): {r.text} — /kakao/login 으로 다시 연결하세요.")
    rec = _token_record(r.json(), prev=tok)
    await storage.write(TOKEN_FILE, rec)  # refresh_token 이 바뀌었으면 함께 덮어씀
    log.info("Kakao access token refreshed (refresh_token rotated=%s)", "refresh_token" in r.json())
    return rec["access_token"]


async def _kakao_call(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    """401 이면 토큰을 강제 갱신하고 1회 재시도"""
    for attempt in (0, 1):
        token = await kakao_access_token(client, force_refresh=attempt == 1)
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        r = await client.request(method, f"{KAKAO_API_HOST}{path}", headers=headers, **kwargs)
        if r.status_code == 401 and attempt == 0:
            continue
        if r.status_code != 200:
            raise KakaoError(f"{path} 실패({r.status_code}): {r.text}")
        return r.json()
    raise KakaoError(f"{path} 실패: 인증 오류")


async def kakao_upload_image(client: httpx.AsyncClient, png: bytes) -> dict:
    """POST /v2/api/talk/message/image/upload (multipart 'file', 5MB 이하, 카카오 서버 최대 100일 보관)"""
    data = await _kakao_call(
        client, "POST", "/v2/api/talk/message/image/upload",
        files={"file": ("planner.png", png, "image/png")},
    )
    return data["infos"]["original"]  # {url, length, content_type, width, height}


async def kakao_send_memo(client: httpx.AsyncClient, template: dict) -> dict:
    """POST /v2/api/talk/memo/default/send (나에게 보내기, 기본 템플릿)"""
    data = await _kakao_call(
        client, "POST", "/v2/api/talk/memo/default/send",
        data={"template_object": json.dumps(template, ensure_ascii=False)},
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    if data.get("result_code") != 0:
        raise KakaoError(f"memo send failed: {data}")
    return data


async def kakao_connected() -> dict:
    tok = await storage.read(TOKEN_FILE)
    if not tok or not tok.get("refresh_token"):
        return {"connected": False}
    rexp = tok.get("refresh_token_expires_at")
    return {
        "connected": True,
        "refresh_token_expires_at": datetime.fromtimestamp(rexp, TZ).isoformat(timespec="minutes") if rexp else None,
        "updated_at": tok.get("updated_at"),
    }


# ──────────────────────────────────────────────────────────────────────────────
# ntfy 푸시
# ──────────────────────────────────────────────────────────────────────────────
def rfc2047(s: str) -> str:
    """ntfy 헤더에 한글을 넣기 위한 RFC 2047 인코딩"""
    try:
        s.encode("ascii")
        return s
    except UnicodeEncodeError:
        return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode("ascii") + "?="


async def ntfy_push(client: httpx.AsyncClient, title: str, message: str, *, click: str = "kakaotalk://",
                    priority: str = "high", tags: str = "calendar") -> bool:
    if not NTFY_TOPIC:
        log.info("NTFY_TOPIC not set — skip push")
        return False
    headers = {
        "Title": rfc2047(title),
        "Priority": priority,
        "Tags": tags,
        "Click": click,
    }
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    r = await client.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", content=message.encode("utf-8"), headers=headers)
    if r.status_code >= 300:
        log.warning("ntfy failed: %s %s", r.status_code, r.text)
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# 캡처 & 상태바 합성
# ──────────────────────────────────────────────────────────────────────────────
_capture_lock = asyncio.Lock()


async def capture_planner(d: str) -> bytes:
    from playwright.async_api import async_playwright

    url = f"{INTERNAL_BASE_URL}/?key={quote(APP_SECRET_KEY, safe='')}&date={d}&capture=1"
    async with _capture_lock, async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROMIUM_EXECUTABLE,
            args=["--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            ctx = await browser.new_context(
                viewport={"width": CAPTURE_WIDTH, "height": CAPTURE_HEIGHT},
                device_scale_factor=CAPTURE_SCALE,
                is_mobile=True,
                has_touch=True,
                color_scheme="dark",
                locale="ko-KR",
                timezone_id=str(TZ),
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await page.wait_for_selector("body[data-ready='1']", timeout=20_000)
            await page.evaluate("document.fonts.ready.then(() => true)")
            return await page.screenshot(type="png", full_page=False)
        finally:
            await browser.close()


def _load_font(size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(STATUS_FONT_PATH, size)
    except OSError:
        log.warning("status bar font not found (%s) — fallback to default", STATUS_FONT_PATH)
        return ImageFont.load_default(size=size)


def status_bar_values(now: datetime) -> tuple[str, int]:
    """캡처에 찍을 (시각, 배터리%). 기본은 캡처마다 랜덤."""
    if STATUS_TIME_MODE != "random":
        return now.strftime("%H:%M"), STATUS_BATTERY_FIXED
    rng = random.SystemRandom()
    try:
        t0, t1 = (hhmm_to_min(x.strip()) for x in STATUS_TIME_RANGE.split("-"))
    except ValueError:
        t0, t1 = 0, 24 * 60 - 1
    span = (t1 - t0) % (24 * 60)  # 22:00-02:00 처럼 자정을 넘는 범위도 허용
    m = (t0 + rng.randint(0, span)) % (24 * 60)
    try:
        b0, b1 = sorted(max(1, min(100, int(x))) for x in STATUS_BATTERY_RANGE.split("-"))
    except ValueError:
        b0, b1 = 20, 100
    return f"{m // 60:02d}:{m % 60:02d}", rng.randint(b0, b1)


def render_status_bar(width: int, hhmm: str, battery: int = 89) -> Image.Image:
    """아이폰 다크모드 상태바 (2x 기준 width × 104px). 4배 슈퍼샘플링 후 축소해 안티에일리어싱."""
    ss = 4
    s = CAPTURE_SCALE * ss  # pt → 슈퍼샘플 픽셀
    W, H = width * ss, STATUS_BAR_HEIGHT_PX * ss
    img = Image.new("RGB", (W, H), STATUS_BAR_BG)
    dr = ImageDraw.Draw(img)
    white = (255, 255, 255)
    cy = H / 2

    # 시각 (좌측 영역 중앙, SF 17pt semibold 느낌)
    f_time = _load_font(int(17 * s))
    dr.text((55 * s, cy), hhmm, font=f_time, fill=white, anchor="mm")

    # 오른쪽부터 배치: 배터리 → LTE → 안테나
    right = W - 26 * s

    # 배터리: 몸통 25×12pt, 캡 1.5×4.5pt
    cap_w, cap_h = 1.6 * s, 4.5 * s
    body_w, body_h = 25 * s, 12 * s
    bx1 = right - cap_w - 1.2 * s
    bx0 = bx1 - body_w
    by0, by1 = cy - body_h / 2, cy + body_h / 2
    radius = 3.6 * s
    battery = max(1, min(battery, 100))
    dr.rounded_rectangle((bx0, by0, bx1, by1), radius=radius, fill=(88, 88, 90))
    fill_x = bx0 + body_w * battery / 100
    fill_color = (255, 59, 48) if battery <= 20 else white  # iOS: 20% 이하 빨간색
    # 채워진 부분만 흰색 (둥근 모서리 유지 위해 마스크 사용)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((bx0, by0, bx1, by1), radius=radius, fill=255)
    clip = Image.new("L", (W, H), 0)
    ImageDraw.Draw(clip).rectangle((bx0, by0, fill_x, by1), fill=255)
    from PIL import ImageChops

    fill_mask = ImageChops.multiply(mask, clip)
    img.paste(fill_color, (0, 0, W, H), fill_mask)
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle((bx1 + 1.0 * s, cy - cap_h / 2, bx1 + 1.0 * s + cap_w, cy + cap_h / 2),
                         radius=0.8 * s, fill=(88, 88, 90))
    f_batt = _load_font(int(11.5 * s))
    # 숫자: 채워진 부분 위는 어둡게(뚫린 느낌), 빈 부분 위는 흰색
    txt = Image.new("L", (W, H), 0)
    ImageDraw.Draw(txt).text(((bx0 + bx1) / 2, cy + 0.2 * s), str(battery), font=f_batt, fill=255, anchor="mm")
    img.paste(STATUS_BAR_BG, (0, 0, W, H), ImageChops.multiply(txt, fill_mask))
    img.paste(white, (0, 0, W, H), ImageChops.multiply(txt, ImageChops.invert(fill_mask)))
    dr = ImageDraw.Draw(img)

    # LTE
    f_lte = _load_font(int(15 * s))
    lte_right = bx0 - 6.5 * s
    dr.text((lte_right, cy + 0.3 * s), "LTE", font=f_lte, fill=white, anchor="rm")
    lte_w = dr.textlength("LTE", font=f_lte)

    # 안테나 4칸 (아래 정렬, 높이 증가)
    bar_w, gap = 3.0 * s, 1.7 * s
    heights = [4.2 * s, 6.4 * s, 8.8 * s, 11.2 * s]
    base_y = cy + heights[-1] / 2
    ax_right = lte_right - lte_w - 5.5 * s
    ax0 = ax_right - (4 * bar_w + 3 * gap)
    for i, h in enumerate(heights):
        x0 = ax0 + i * (bar_w + gap)
        dr.rounded_rectangle((x0, base_y - h, x0 + bar_w, base_y), radius=0.9 * s, fill=white)

    return img.resize((width, STATUS_BAR_HEIGHT_PX), Image.LANCZOS)


def compose_with_status_bar(png: bytes, hhmm: str, battery: int) -> bytes:
    shot = Image.open(io.BytesIO(png)).convert("RGB")
    bar = render_status_bar(shot.width, hhmm, battery)
    shot.paste(bar, (0, 0))  # 캡처 모드 페이지는 상단 52px(=104px@2x)를 비워 둔다
    out = io.BytesIO()
    shot.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def build_brief_image(d: str, now: datetime) -> bytes:
    raw = await capture_planner(d)
    hhmm, battery = status_bar_values(now)
    log.info("status bar: %s / battery %d%%", hhmm, battery)
    return await asyncio.to_thread(compose_with_status_bar, raw, hhmm, battery)


# ──────────────────────────────────────────────────────────────────────────────
# 알림 시나리오
# ──────────────────────────────────────────────────────────────────────────────
async def send_nag(d: str) -> dict:
    """조건 A: 오늘 플래너 미작성 독촉"""
    result: dict[str, Any] = {"type": "nag", "date": d}
    dt = date.fromisoformat(d)
    text = (
        f"⏰ 오늘({dt.month}/{dt.day} {WEEKDAYS_KO[dt.weekday()]}) 플래너가 아직 비어 있어요!\n"
        "10분만 투자해서 오늘 할 일과 시간 계획을 채워볼까요? ✍️"
    )
    link_url = app_url(d, with_key=KAKAO_LINK_INCLUDE_KEY)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            await kakao_send_memo(client, {
                "object_type": "text",
                "text": text,
                "link": {"web_url": link_url, "mobile_web_url": link_url},
                "button_title": "플래너 작성하기",
            })
            result["kakao"] = "ok"
        except Exception as e:  # noqa: BLE001
            log.exception("kakao nag failed")
            result["kakao"] = f"error: {e}"
        result["ntfy"] = await ntfy_push(client, "📝 플래너 작성 알림", "오늘 플래너가 비어 있어요. 지금 작성해 주세요!",
                                         click="kakaotalk://", priority="high", tags="pencil,alarm_clock")
    return result


async def send_brief(d: str, now: datetime) -> dict:
    """조건 B: 완성본 캡처 → 카카오 이미지 업로드 → 나에게 보내기(Feed) → ntfy"""
    result: dict[str, Any] = {"type": "brief", "date": d}
    hhmm = now.strftime("%H:%M")
    plan = await load_plan(d)
    st = plan_stats(plan)
    per = " · ".join(f"{n} {fmt_hms(m)[:-3]}" for n, m in st["per_task"] if m > 0) or "시간 계획 없음"
    desc = f"총 {fmt_hm(st['total_minutes'])} | 할 일 {st['todo_done']}/{st['todo_total']} 완료\n{per}"
    link_url = app_url(d, with_key=KAKAO_LINK_INCLUDE_KEY)

    png = await build_brief_image(d, now)
    result["image_bytes"] = len(png)

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            info = await kakao_upload_image(client, png)
            await kakao_send_memo(client, {
                "object_type": "feed",
                "content": {
                    "title": f"📅 {date_label(d)} 플래너",
                    "description": desc[:200],
                    "image_url": info["url"],
                    "image_width": int(info.get("width") or CAPTURE_WIDTH * CAPTURE_SCALE),
                    "image_height": int(info.get("height") or CAPTURE_HEIGHT * CAPTURE_SCALE),
                    "link": {"web_url": link_url, "mobile_web_url": link_url},
                },
                "buttons": [
                    {"title": "플래너 열기", "link": {"web_url": link_url, "mobile_web_url": link_url}},
                ],
            })
            result["kakao"] = "ok"
            result["image_url"] = info["url"]
        except Exception as e:  # noqa: BLE001
            log.exception("kakao brief failed")
            result["kakao"] = f"error: {e}"

        ok = result["kakao"] == "ok"
        result["ntfy"] = await ntfy_push(
            client,
            f"📅 {hhmm} 플래너 브리핑" if ok else "⚠️ 플래너 카톡 발송 실패",
            (f"총 {fmt_hm(st['total_minutes'])} · 할 일 {st['todo_done']}/{st['todo_total']}\n카카오톡에서 확인하세요."
             if ok else str(result["kakao"])[:300]),
            click="kakaotalk://",
            priority="high",
            tags="calendar" if ok else "warning",
        )
    return result


_cron_lock = asyncio.Lock()


async def run_cron(now: datetime) -> dict:
    today = planner_today(now)
    hhmm = now.strftime("%H:%M")
    settings = await load_settings()
    plan = await load_plan(today)
    state = await storage.read(STATE_FILE) or {}
    sent: dict[str, list[str]] = state.setdefault("sent", {})
    actions: list[dict] = []
    is_empty = not plan.get("tasks")

    if is_empty:
        # 조건 A
        in_window = NAG_START <= hhmm <= NAG_END
        last = state.get("last_nag_at")
        elapsed_ok = True
        if last:
            elapsed_ok = (now - datetime.fromisoformat(last)).total_seconds() >= settings.nag_interval_minutes * 60 - 30
        if settings.nag_enabled and in_window and elapsed_ok:
            state["last_nag_at"] = now.isoformat(timespec="seconds")
            await storage.write(STATE_FILE, state)  # 재시도로 인한 중복 발송 방지 위해 먼저 기록
            actions.append(await send_nag(today))
    else:
        # 조건 B: notify_times 중 '지금' (스케줄러 지연 허용) 이고 아직 안 보낸 것
        now_min = now.hour * 60 + now.minute
        already = set(sent.get(today, []))
        due = [t for t in settings.notify_times
               if 0 <= now_min - hhmm_to_min(t) < max(1, NOTIFY_CATCHUP_MINUTES) and t not in already]
        if due:
            sent[today] = sorted(already | set(due))
            await storage.write(STATE_FILE, state)
            try:
                actions.append(await send_brief(today, now))
            except Exception as e:  # noqa: BLE001  (캡처 실패 등)
                log.exception("brief failed")
                actions.append({"type": "brief", "error": str(e)})
                async with httpx.AsyncClient(timeout=15) as client:
                    await ntfy_push(client, "⚠️ 플래너 캡처 실패", str(e)[:300], tags="warning")

    # 오래된 발송 기록 정리
    cutoff = (now.date() - timedelta(days=7)).isoformat()
    state["sent"] = {k: v for k, v in sent.items() if k >= cutoff}
    state["last_cron_at"] = now.isoformat(timespec="seconds")
    await storage.write(STATE_FILE, state)
    for a in actions:
        a.pop("image_bytes", None)
    return {"now": now.isoformat(timespec="seconds"), "date": today, "time": hhmm,
            "plan_empty": is_empty, "actions": actions}


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI
# ──────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(_: FastAPI):
    global storage
    if not APP_SECRET_KEY:
        raise RuntimeError("APP_SECRET_KEY 환경변수가 필요합니다.")
    storage = Storage()
    yield


app = FastAPI(title="Daily Planner", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/fonts", StaticFiles(directory=STATIC_DIR / "fonts"), name="fonts")

_INDEX_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


@app.get("/", response_class=HTMLResponse)
async def index(key: str | None = Query(default=None)):
    if not key_ok(key):
        return HTMLResponse(
            "<!doctype html><meta name=viewport content='width=device-width'>"
            "<body style='background:#121212;color:#888;font-family:sans-serif;display:grid;place-items:center;height:90vh'>"
            "403 · 접근 권한이 없습니다</body>",
            status_code=403, headers=_NO_STORE,
        )
    return HTMLResponse(_INDEX_HTML, headers=_NO_STORE)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/api/plan/{d}", dependencies=[Depends(require_key)])
async def get_plan(d: str):
    try:
        validate_date(d)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return JSONResponse(await load_plan(d), headers=_NO_STORE)


@app.post("/api/plan", dependencies=[Depends(require_key)])
async def save_plan(plan: Plan):
    data = sanitize_plan(plan)
    await storage.write(plan_file(plan.date), data)
    return {"ok": True, "updated_at": data["updated_at"], "stats": plan_stats(data)}


@app.get("/api/settings", dependencies=[Depends(require_key)])
async def get_settings():
    return (await load_settings()).model_dump()


@app.post("/api/settings", dependencies=[Depends(require_key)])
async def save_settings(settings: Settings):
    await storage.write(SETTINGS_FILE, settings.model_dump())
    return {"ok": True, **settings.model_dump()}


@app.get("/api/status", dependencies=[Depends(require_key)])
async def status():
    now = now_kst()
    state = await storage.read(STATE_FILE) or {}
    return {
        "now": now.isoformat(timespec="seconds"),
        "today": planner_today(now),
        "day_start_hour": DAY_START_HOUR,
        "storage": storage.mode,
        "kakao": {"configured": bool(KAKAO_REST_API_KEY), **(await kakao_connected())},
        "ntfy": {"configured": bool(NTFY_TOPIC), "server": NTFY_SERVER},
        "nag_window": [NAG_START, NAG_END],
        "last_cron_at": state.get("last_cron_at"),
    }


@app.post("/api/cron", dependencies=[Depends(require_key)])
async def cron():
    async with _cron_lock:
        return await run_cron(now_kst())


@app.post("/api/notify/test", dependencies=[Depends(require_key)])
async def notify_test(kind: Literal["brief", "nag"] = Body("brief", embed=True),
                      d: str | None = Body(None, embed=True, alias="date")):
    now = now_kst()
    target = validate_date(d) if d else planner_today(now)
    if kind == "nag":
        return await send_nag(target)
    res = await send_brief(target, now)
    res.pop("image_bytes", None)
    return res


@app.get("/api/preview.png", dependencies=[Depends(require_key)])
async def preview_png(d: str | None = Query(default=None, alias="date")):
    now = now_kst()
    target = validate_date(d) if d else planner_today(now)
    png = await build_brief_image(target, now)
    return Response(png, media_type="image/png", headers=_NO_STORE)


@app.get("/kakao/login")
async def kakao_login(key: str | None = Query(default=None)):
    if not key_ok(key):
        raise HTTPException(401, "invalid key")
    if not KAKAO_REST_API_KEY:
        raise HTTPException(500, "KAKAO_REST_API_KEY 가 설정되지 않았습니다.")
    params = {
        "response_type": "code",
        "client_id": KAKAO_REST_API_KEY,
        "redirect_uri": KAKAO_REDIRECT_URI,
        "scope": "talk_message",
        "state": sign_state(int(time.time())),
    }
    return RedirectResponse(f"{KAKAO_AUTH_HOST}/oauth/authorize?{urlencode(params)}")


@app.get("/kakao/callback", response_class=HTMLResponse)
async def kakao_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                         error_description: str | None = None):
    if error:
        return HTMLResponse(f"카카오 인증 실패: {error} {error_description or ''}", status_code=400)
    if not code or not state or not verify_state(state):
        return HTMLResponse("잘못된 요청입니다 (state 불일치 또는 만료).", status_code=400)
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            rec = await kakao_exchange_code(client, code)
        except KakaoError as e:
            return HTMLResponse(f"토큰 발급 실패: {e}", status_code=400)
    scope = rec.get("scope", "")
    return HTMLResponse(
        "<!doctype html><meta name=viewport content='width=device-width'>"
        "<body style='background:#121212;color:#eee;font-family:sans-serif;padding:40px 24px;line-height:1.6'>"
        "<h2>✅ 카카오톡 연결 완료</h2>"
        f"<p>권한(scope): {scope or '-'}</p>"
        "<p>이제 플래너 화면으로 돌아가 알림 설정에서 <b>테스트 발송</b>을 눌러 보세요.</p>"
        "<button onclick='history.go(-2)' style='padding:10px 16px;border-radius:10px'>돌아가기</button></body>",
        headers=_NO_STORE,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
