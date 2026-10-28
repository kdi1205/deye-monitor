#!/usr/bin/env python3
"""
Deye Cloud OpenAPI 수집기 — 5kW 목업 모니터링

하는 일 (한 번 실행에 한 사이클):
  1. 토큰 확보: 캐시 파일(.cache/token.json)이 유효하면 재사용, 아니면 재발급
  2. 발전소·장비 목록 확보: 캐시(.cache/inventory.json)가 있으면 재사용
  3. 발전소 최신값(/station/latest) + 인버터 최신값(/device/latest) 조회
  4. data/latest.json 덮어쓰기
  5. data/history/YYYY-MM-DD.json 에 한 행 추가 (KST 기준 날짜)
  6. data/daily.json 일간 집계 갱신
  7. Deye 에 남은 오늘 5분 기록으로 빈 시각 보충 (GitHub 예약 실행이 밀려도 하루 곡선이 촘촘하게)
  실패하면 이전 latest.json 을 유지하고 status 필드에 오류를 기록한다.

환경변수 (GitHub Secrets 로 주입):
  DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD   (필수)
  DEYE_REGION      eu | us | india   (기본 eu — 아시아태평양은 eu1 서버)
  DEYE_STATION_ID  발전소가 여러 개면 지정 (선택)
  DEYE_DEVICE_SN   인버터 SN 을 직접 지정 (선택)
  DATA_PASSPHRASE  데이터 암호화 비밀번호. 설정하면 data/ 와 .cache/ 의 모든 파일을
                   AES-256-GCM(키: PBKDF2-SHA256 60만 회)으로 암호화해 저장한다.
                   화면(index.html)은 같은 비밀번호를 넣어야 열린다.
  DATA_PASSPHRASE_OLD  비밀번호를 바꿀 때 한 번만 이전 비밀번호를 넣는다 (기존 파일 재암호화)
  DEYE_REQUIRE_ENCRYPTION=1  DATA_PASSPHRASE 가 없으면 평문으로 쓰지 않고 실패한다 (Actions 에서 사용)

사용:
  python collector.py            수집 1회
  python collector.py --check    토큰 발급 + 발전소/장비 목록만 출력 (처음 설정 확인용)
  python collector.py --points   인버터의 measurePoints 목록 출력 (필드 이름 확인용)
  python collector.py --backfill [시작일] [종료일] [--force]
                                 Deye Cloud 의 과거 기록을 받아 history/·daily.json 에 채운다
                                 (날짜 YYYY-MM-DD, 생략하면 30일 전 ~ 오늘)

암호화를 쓰면 cryptography 패키지가 필요하다 (pip install cryptography). 그 외는 표준 라이브러리.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import unicodedata
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HIST = DATA / "history"
CACHE = ROOT / ".cache"
KST = ZoneInfo("Asia/Seoul")

REGIONS = {
    "eu": "https://eu1-developer.deyecloud.com/v1.0",      # Europe, EMEA, Asia-Pacific
    "us": "https://us1-developer.deyecloud.com/v1.0",      # Americas
    "india": "https://india-developer.deyecloud.com/v1.0",
}
SUCCESS = {0, 1000000, 1106000, "0", "1000000", "1106000"}
ENVELOPE = {"code", "msg", "success", "requestId"}
TIMEOUT = 30
HISTORY_KEEP_DAYS = int(os.environ.get("DEYE_HISTORY_KEEP_DAYS") or 3650)   # history 파일 보관 일수
DAILY_KEEP_DAYS = int(os.environ.get("DEYE_DAILY_KEEP_DAYS") or 3650)       # daily.json 보관 일수
API_GAP_S = 1.0             # 과거 데이터 조회 시 호출 간격 (호출 제한 대비)

# /device/latest 의 dataList key → latest.json 의 정규화 이름. 앞의 것부터 찾는다.
FIELD_MAP: dict[str, list[str]] = {
    "pv1_power_w":      ["DCPowerPV1"],
    "pv2_power_w":      ["DCPowerPV2"],
    "pv_power_w":       ["TotalDCInputPower"],
    "pv1_voltage_v":    ["DCVoltagePV1"],
    "pv2_voltage_v":    ["DCVoltagePV2"],
    "pv1_current_a":    ["DCCurrentPV1"],
    "pv2_current_a":    ["DCCurrentPV2"],
    "battery_soc_pct":  ["SOC"],
    "battery_voltage_v": ["BatteryVoltage"],
    "battery_current_a": ["BatteryCurrent"],
    "battery_power_w":  ["BatteryPower"],
    "battery_temp_c":   ["Temperature- Battery", "BatteryTemperature"],
    "load_power_w":     ["TotalConsumptionPower", "UPSLoadPower"],
    "load_voltage_v":   ["LoadVoltageL1L2", "ACVoltageRUA"],
    "ac_frequency_hz":  ["ACOutputFrequencyR"],
    "inverter_temp_dc_c": ["DC Temperature"],
    "inverter_temp_ac_c": ["AC Temperature"],
    "pv_today_kwh":     ["PVDailyPowerGenerationActive", "DailyActiveProduction"],
    "pv_total_kwh":     ["PVCumulativePowerGenerationActive", "TotalActiveProduction"],
    "load_today_kwh":   ["DailyConsumption"],
    "load_total_kwh":   ["CumulativeConsumption"],
    "charge_today_kwh": ["DailyChargingEnergy"],
    "discharge_today_kwh": ["DailyDischargingEnergy"],
    "charge_total_kwh": ["TotalChargeEnergy"],
    "discharge_total_kwh": ["TotalDischargeEnergy"],
}
# history 행에 남길 항목 (파일 크기를 위해 핵심만)
HISTORY_FIELDS = [
    "pv_power_w", "pv1_power_w", "pv2_power_w", "battery_soc_pct", "battery_power_w",
    "battery_voltage_v", "battery_temp_c", "load_power_w", "load_voltage_v",
    "pv_today_kwh", "load_today_kwh", "charge_today_kwh", "discharge_today_kwh",
]


# ───────────────────────── 공통 유틸 ─────────────────────────
def now_kst() -> datetime:
    return datetime.now(KST)


class ApiError(Exception):
    pass


# ───────────────────────── 암호화 ─────────────────────────
# 파일 형식 3가지 (AES-256-GCM, 키: PBKDF2-SHA256)
#   통째: {"enc":"aesgcm-pbkdf2-v1", "iv", "ct"}                AAD = 경로            (latest.json, .cache/*)
#   행별: {"enc":"aesgcm-rows-v1", "rows":[{"iv","ct"}, ...]}    AAD = 경로 + "#row"   (history/*.json)
#   날짜별: {"enc":"aesgcm-map-v1", "items":{"YYYY-MM-DD":{"iv","ct"}}}  AAD = 경로 + "#날짜"  (daily.json)
# 행별·날짜별로 나누는 이유: 통째로 암호화하면 5분마다 파일 전체가 새 난수로 바뀌어 git 이 차분 저장을 못 한다
#   (시험: 하루 288커밋에 15.6MB → 행별 1.5MB). 내용이 같은 행은 같은 암호문을 다시 써서 바뀐 행만 커밋된다.
#   같은 평문·같은 AAD 에만 같은 iv 를 재사용하므로 새로 드러나는 정보는 없다 (그 행이 안 바뀌었다는 것뿐).
# 키 정보: data/key.json = {"v":1, "kdf":"PBKDF2-SHA256", "iter":600000, "salt":b64, "check":<봉투>}
#   salt 와 iter 는 공개되어도 된다. check 는 브라우저가 비밀번호를 빠르게 검증하는 용도.
KEY_FILE = DATA / "key.json"
KDF_ITER = 600_000
ENC_TAG = "aesgcm-pbkdf2-v1"
ROWS_TAG = "aesgcm-rows-v1"
MAP_TAG = "aesgcm-map-v1"
CHECK_TEXT = "deye-monitor-ok"
CRYPTO = None   # 암호화 사용 시 Crypto 인스턴스


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _is_env(obj) -> bool:
    return isinstance(obj, dict) and obj.get("enc") in (ENC_TAG, ROWS_TAG, MAP_TAG)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _aad(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def _kind(path: Path) -> str:
    rel = _aad(path)
    if rel.startswith("data/history/"):
        return "rows"
    if rel == "data/daily.json":
        return "map"
    return "whole"


class Crypto:
    def __init__(self, passphrase: str, old_passphrase: str | None = None) -> None:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes
        except ImportError:
            raise ApiError("암호화에 cryptography 패키지가 필요합니다: python3 -m pip install --user cryptography")
        self._AESGCM, self._PBKDF2, self._hashes = AESGCM, PBKDF2HMAC, hashes
        meta = None
        if KEY_FILE.exists():
            meta = json.loads(KEY_FILE.read_text(encoding="utf-8"))
        if not meta:
            meta = {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITER, "salt": _b64e(secrets.token_bytes(16))}
        self.meta = meta
        self.salt = _b64d(meta["salt"])
        self.iter = int(meta.get("iter", KDF_ITER))
        self.key = self._derive(passphrase)
        self.old_key = self._derive(old_passphrase) if old_passphrase else None
        self._reuse: dict[tuple[str, str], dict] = {}   # (aad, 평문) → 기존 암호문 (바뀌지 않은 행 재사용)
        self.used_old = False
        changed = "check" not in meta
        if "check" in meta:
            if self._try(meta["check"], "check", self.key) == CHECK_TEXT:
                pass
            elif self.old_key and self._try(meta["check"], "check", self.old_key) == CHECK_TEXT:
                changed = True       # 비밀번호 교체
            else:
                raise ApiError("DATA_PASSPHRASE 가 기존 데이터와 맞지 않습니다. 비밀번호를 바꾼 경우 "
                               "DATA_PASSPHRASE_OLD 에 이전 비밀번호를 넣고 한 번 실행하세요.")
        if changed:
            iv = secrets.token_bytes(12)
            ct = self._AESGCM(self.key).encrypt(iv, CHECK_TEXT.encode(), b"check")
            meta["check"] = {"enc": ENC_TAG, "iv": _b64e(iv), "ct": _b64e(ct)}
            DATA.mkdir(parents=True, exist_ok=True)
            KEY_FILE.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        self._migrate()

    def _derive(self, pw: str) -> bytes:
        pw = unicodedata.normalize("NFC", pw)
        kdf = self._PBKDF2(algorithm=self._hashes.SHA256(), length=32, salt=self.salt, iterations=self.iter)
        return kdf.derive(pw.encode("utf-8"))

    def _try(self, env: dict, aad: str, key: bytes):
        try:
            return self._AESGCM(key).decrypt(_b64d(env["iv"]), _b64d(env["ct"]), aad.encode("utf-8")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None

    def _open(self, env: dict, aad: str) -> str:
        txt = self._try(env, aad, self.key)
        if txt is not None:
            self._reuse[(aad, txt)] = {"iv": env["iv"], "ct": env["ct"]}
            return txt
        if self.old_key:
            txt = self._try(env, aad, self.old_key)
            if txt is not None:
                self.used_old = True
                return txt
        raise ApiError(f"복호화 실패: {aad} (비밀번호가 다르거나 파일이 손상됨)")

    def _seal(self, plain: str, aad: str) -> dict:
        hit = self._reuse.get((aad, plain))
        if hit:
            return hit
        iv = secrets.token_bytes(12)
        ct = self._AESGCM(self.key).encrypt(iv, plain.encode("utf-8"), aad.encode("utf-8"))
        env = {"iv": _b64e(iv), "ct": _b64e(ct)}
        self._reuse[(aad, plain)] = env
        return env

    def encode(self, obj, path: Path) -> str:
        """평문 객체 → 파일에 쓸 문자열. 행별·날짜별 형식은 한 줄에 한 항목 (diff 가 읽기 쉽다)."""
        aad, kind = _aad(path), _kind(path)
        if kind == "rows" and isinstance(obj, list):
            body = ",\n".join(json.dumps(self._seal(_dump(r), aad + "#row")) for r in obj)
            return '{"enc":"%s","rows":[\n%s\n]}\n' % (ROWS_TAG, body)
        if kind == "map" and isinstance(obj, dict):
            body = ",\n".join(f"{json.dumps(k)}:{json.dumps(self._seal(_dump(v), f'{aad}#{k}'))}"
                              for k, v in sorted(obj.items()))
            return '{"enc":"%s","items":{\n%s\n}}\n' % (MAP_TAG, body)
        return json.dumps({"enc": ENC_TAG, **self._seal(_dump(obj), aad)})

    def decode(self, fobj: dict, path: Path):
        aad, tag = _aad(path), fobj.get("enc")
        if tag == ROWS_TAG:
            return [json.loads(self._open(e, aad + "#row")) for e in fobj.get("rows") or []]
        if tag == MAP_TAG:
            return {k: json.loads(self._open(e, f"{aad}#{k}")) for k, e in (fobj.get("items") or {}).items()}
        return json.loads(self._open(fobj, aad))

    def _first_env(self, fobj: dict, path: Path):
        tag = fobj.get("enc")
        if tag == ROWS_TAG:
            rows = fobj.get("rows") or []
            return (rows[0], _aad(path) + "#row") if rows else (None, None)
        if tag == MAP_TAG:
            items = fobj.get("items") or {}
            k = next(iter(items), None)
            return (items[k], f"{_aad(path)}#{k}") if k else (None, None)
        return fobj, _aad(path)

    def _migrate(self) -> None:
        """평문 파일, 이전 비밀번호로 암호화된 파일, 형식이 맞지 않는 파일(예: 통째 암호화된 history)을
        현재 키·현재 형식으로 다시 쓴다. 정상 파일은 첫 항목만 확인하고 넘어간다."""
        targets = [p for p in list(DATA.rglob("*.json")) + list(CACHE.glob("*.json")) if p != KEY_FILE]
        want = {"rows": ROWS_TAG, "map": MAP_TAG, "whole": ENC_TAG}
        for p in targets:
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _is_env(obj):
                env, aad = self._first_env(obj, p)
                if obj.get("enc") == want[_kind(p)] and (env is None or self._try(env, aad, self.key) is not None):
                    continue
                try:
                    plain = self.decode(obj, p)      # 안 풀리면 예외
                except ApiError:
                    if CACHE in p.parents:           # 캐시(토큰·장비 목록)는 다시 받으면 되므로 지운다
                        p.unlink(missing_ok=True)
                        continue
                    raise
            else:
                plain = obj                          # 평문 → 암호화
            p.write_text(self.encode(plain, p), encoding="utf-8")


def init_crypto() -> None:
    global CRYPTO
    pw = os.environ.get("DATA_PASSPHRASE", "")
    if pw:
        CRYPTO = Crypto(pw, os.environ.get("DATA_PASSPHRASE_OLD") or None)
    elif os.environ.get("DEYE_REQUIRE_ENCRYPTION") == "1":
        raise ApiError("DEYE_REQUIRE_ENCRYPTION=1 인데 DATA_PASSPHRASE 가 없습니다. 평문으로 저장하지 않고 중단합니다.")


def read_json(path: Path, default):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    if _is_env(obj):
        if CRYPTO is None:
            raise ApiError(f"{_aad(path)} 는 암호화되어 있습니다. DATA_PASSPHRASE 를 설정하세요.")
        try:
            return CRYPTO.decode(obj, path)
        except ApiError:
            if CACHE in path.resolve().parents:  # 캐시는 못 풀면 없는 것으로 보고 다시 받는다
                return default
            raise
    return obj


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if CRYPTO is not None and path != KEY_FILE:
        text = CRYPTO.encode(obj, path)
    else:
        text = json.dumps(obj, ensure_ascii=False, indent=1)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def to_num(v):
    """API 값은 문자열로 오는 경우가 많다. 숫자로 바꾸고, 안 되면 None."""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return int(f) if f.is_integer() and abs(f) < 1e12 else round(f, 3)
    except (TypeError, ValueError):
        return None


# ───────────────────────── Deye Cloud 클라이언트 ─────────────────────────
class DeyeClient:
    def __init__(self) -> None:
        self.app_id = os.environ.get("DEYE_APP_ID", "").strip()
        self.app_secret = os.environ.get("DEYE_APP_SECRET", "").strip()
        self.email = os.environ.get("DEYE_EMAIL", "").strip()
        self.password = os.environ.get("DEYE_PASSWORD", "")
        region = os.environ.get("DEYE_REGION", "eu").strip().lower()
        self.base = REGIONS.get(region, REGIONS["eu"])
        self.token: str | None = None
        self.token_exp: float = 0.0
        missing = [k for k, v in (("DEYE_APP_ID", self.app_id), ("DEYE_APP_SECRET", self.app_secret),
                                  ("DEYE_EMAIL", self.email), ("DEYE_PASSWORD", self.password)) if not v]
        if missing:
            raise ApiError("환경변수 누락: " + ", ".join(missing))

    # --- 토큰 ---
    def load_token(self) -> None:
        c = read_json(CACHE / "token.json", {})
        if c.get("token") and c.get("exp", 0) > time.time() + 3600 and c.get("base") == self.base:
            self.token, self.token_exp = c["token"], c["exp"]

    def obtain_token(self) -> None:
        pw = hashlib.sha256(self.password.encode("utf-8")).hexdigest().lower()
        body = {"appSecret": self.app_secret, "email": self.email, "password": pw}
        res = self._post(f"/account/token?appId={self.app_id}", body, auth=False)
        tok = res.get("accessToken")
        if not tok:
            raise ApiError(f"토큰 없음: {res}")
        self.token = tok.split(" ", 1)[-1]
        expires_in = float(res.get("expiresIn") or 60 * 24 * 3600)
        self.token_exp = time.time() + expires_in - 86400  # 하루 여유를 두고 만료 처리
        write_json(CACHE / "token.json", {"token": self.token, "exp": self.token_exp, "base": self.base})

    def ensure_token(self) -> None:
        self.load_token()
        if not self.token:
            self.obtain_token()

    # --- HTTP ---
    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        url = self.base + path
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429:
                raise ApiError(f"호출 제한(429) {path}: {detail}")
            raise ApiError(f"HTTP {e.code} {path}: {detail}")
        except urllib.error.URLError as e:
            raise ApiError(f"연결 실패 {path}: {e.reason}")
        try:
            res = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(f"JSON 아님 {path}: {raw[:200]}")
        code = res.get("code")
        if code is not None and code not in SUCCESS:
            msg = str(res.get("msg", ""))
            if code in (1001, 1002, 1003, 2101017, "1001", "1002", "1003", "2101017"):
                # 인증 오류 → 토큰 캐시 폐기
                (CACHE / "token.json").unlink(missing_ok=True)
                raise ApiError(f"인증 오류 {code}: {msg}")
            raise ApiError(f"API 오류 {code} {path}: {msg}")
        return res

    def post(self, path: str, body: dict | None = None) -> dict:
        self.ensure_token()
        try:
            return self._post(path, body or {})
        except ApiError as e:
            if "인증 오류" in str(e):
                self.obtain_token()
                return self._post(path, body or {})
            raise

    # --- 조회 ---
    def stations_with_devices(self) -> list[dict]:
        res = self.post("/station/listWithDevice", {"page": 1, "size": 20})
        data = res.get("data") if isinstance(res.get("data"), dict) else res
        return list(data.get("stationList") or [])

    def station_latest(self, station_id) -> dict:
        res = self.post("/station/latest", {"stationId": station_id})
        data = res.get("data") if isinstance(res.get("data"), dict) else res
        return {k: v for k, v in data.items() if k not in ENVELOPE}

    def device_latest(self, sns: list[str]) -> dict[str, dict]:
        res = self.post("/device/latest", {"deviceList": sns[:10]})
        # 실제 응답(2026-10 확인): 최상위에 "deviceDataList". 문서·구버전은 data.deviceList 라서 둘 다 받는다.
        data = res.get("data") if isinstance(res.get("data"), dict) else res
        devices = (data.get("deviceDataList") or data.get("deviceList")
                   or res.get("deviceDataList") or res.get("deviceList") or [])
        out = {}
        for dev in devices:
            sn = dev.get("deviceSn")
            if not sn:
                continue
            values, units = {}, {}
            for item in dev.get("dataList") or []:
                k = item.get("key")
                if k:
                    values[k] = item.get("value")
                    if item.get("unit"):
                        units[k] = item["unit"]
            out[sn] = {"values": values, "units": units,
                       "collection_time": dev.get("collectionTime"),
                       "device_state": dev.get("deviceState"), "raw": dev}
        return out

    def measure_points(self, sn: str, device_type: str = "INVERTER") -> list:
        res = self.post("/device/measurePoints", {"deviceSn": sn, "deviceType": device_type})
        data = res.get("data") if isinstance(res.get("data"), dict) else res
        return list(data.get("measurePoints") or [])

    # --- 과거 데이터 ---
    def post_slow(self, path: str, body: dict) -> dict:
        """과거 데이터용: 호출 간격을 두고, 호출 제한(429)이면 30초 쉬고 한 번 더."""
        time.sleep(API_GAP_S)
        try:
            return self.post(path, body)
        except ApiError as e:
            if "호출 제한" not in str(e):
                raise
            time.sleep(30)
            return self.post(path, body)

    def device_history_daily(self, sn: str, start: str, end: str) -> dict:
        # granularity=2: 일 합계. measurePoints 를 보내면 2101006 오류, 범위는 31일 이하
        return self.post_slow("/device/history", {"deviceSn": sn, "startAt": start, "endAt": end, "granularity": 2})

    def device_history_frames(self, sn: str, day: str, points: list[str]) -> dict:
        # granularity=1: 하루치 프레임(로거 업로드 주기). measurePoints 필수
        return self.post_slow("/device/history", {"deviceSn": sn, "startAt": day, "endAt": day,
                                                  "granularity": 1, "measurePoints": points})

    def station_history_daily(self, station_id, start: str, end: str) -> dict:
        return self.post_slow("/station/history", {"stationId": station_id, "startAt": start, "endAt": end,
                                                   "granularity": 2})


# ───────────────────────── 발전소·장비 선택 ─────────────────────────
def resolve_inventory(client: DeyeClient) -> dict:
    """캐시가 있으면 쓰고, 없으면 API 로 찾아서 캐시한다."""
    inv = read_json(CACHE / "inventory.json", {})
    want_station = os.environ.get("DEYE_STATION_ID", "").strip()
    want_sn = os.environ.get("DEYE_DEVICE_SN", "").strip()
    if inv.get("station_id") and inv.get("device_sn") and \
            (not want_station or str(inv["station_id"]) == want_station) and \
            (not want_sn or inv["device_sn"] == want_sn):
        return inv

    stations = client.stations_with_devices()
    if not stations:
        raise ApiError("발전소가 없습니다. Deye Cloud 앱에서 발전소와 로거를 먼저 등록하세요.")
    station = None
    for s in stations:
        sid = str(s.get("id") or s.get("stationId") or "")
        if not want_station or sid == want_station:
            station = s
            break
    if station is None:
        raise ApiError(f"DEYE_STATION_ID={want_station} 에 해당하는 발전소가 없습니다.")
    devices = station.get("deviceListItems") or []
    inverter = None
    for d in devices:
        if want_sn and d.get("deviceSn") == want_sn:
            inverter = d
            break
        if not want_sn and str(d.get("deviceType", "")).upper() == "INVERTER":
            inverter = d
            break
    if inverter is None and devices and not want_sn:
        inverter = devices[0]
    if inverter is None:
        raise ApiError("인버터 장비를 찾지 못했습니다. --check 로 목록을 확인하세요.")
    inv = {
        "station_id": station.get("id") or station.get("stationId"),
        "station_name": station.get("name"),
        "device_sn": inverter.get("deviceSn"),
        "device_type": inverter.get("deviceType"),
        "device_name": inverter.get("name") or inverter.get("deviceName"),
        "cached_at": now_kst().isoformat(timespec="seconds"),
    }
    write_json(CACHE / "inventory.json", inv)
    return inv


# ───────────────────────── 수집 1사이클 ─────────────────────────
def normalize(values: dict) -> dict:
    out = {}
    for name, keys in FIELD_MAP.items():
        for k in keys:
            if k in values:
                out[name] = to_num(values[k])
                break
        else:
            out[name] = None
    # 센서가 없는 온도는 -40.5 같은 하한값으로 온다 (예: 이 모델의 "DC Temperature") → 빈 값 처리
    for k in ("battery_temp_c", "inverter_temp_dc_c", "inverter_temp_ac_c"):
        if out.get(k) is not None and out[k] <= -40:
            out[k] = None
    # 합계 PV 가 없으면 PV1+PV2 로 계산
    if out.get("pv_power_w") is None and (out.get("pv1_power_w") is not None or out.get("pv2_power_w") is not None):
        out["pv_power_w"] = (out.get("pv1_power_w") or 0) + (out.get("pv2_power_w") or 0)
    return out


def collect() -> int:
    t0 = now_kst()
    try:
        init_crypto()
        prev = read_json(DATA / "latest.json", {})
    except ApiError as e:
        # 비밀번호 불일치 등: 기존 데이터를 덮어쓰지 않도록 아무것도 쓰지 않고 실패 처리
        print(f"::error::{e}")
        return 1
    status = {"ok": False, "checked_at": t0.isoformat(timespec="seconds"), "error": None,
              "consecutive_failures": int(prev.get("status", {}).get("consecutive_failures", 0))}
    try:
        client = DeyeClient()
        inv = resolve_inventory(client)
        st = client.station_latest(inv["station_id"])
        dev = client.device_latest([inv["device_sn"]]).get(inv["device_sn"], {})
        values = dev.get("values", {})
        norm = normalize(values)

        # 발전소 요약값 (인버터 값이 비었을 때의 대체)
        station_summary = {
            "generation_power_w": to_num(st.get("generationPower")),
            "consumption_power_w": to_num(st.get("consumptionPower")),
            "battery_power_w": to_num(st.get("batteryPower")),
            "battery_soc_pct": to_num(st.get("batterySOC")),
            "charge_power_w": to_num(st.get("chargePower")),
            "discharge_power_w": to_num(st.get("dischargePower")),
            "last_update_time": st.get("lastUpdateTime") or st.get("collectionTime"),
        }
        if norm.get("pv_power_w") is None:
            norm["pv_power_w"] = station_summary["generation_power_w"]
        if norm.get("load_power_w") is None:
            norm["load_power_w"] = station_summary["consumption_power_w"]
        if norm.get("battery_soc_pct") is None:
            norm["battery_soc_pct"] = station_summary["battery_soc_pct"]

        # 로거 데이터 시각 (초 단위 unix 또는 문자열)
        ct = dev.get("collection_time") or station_summary["last_update_time"]
        data_time = None
        if ct is not None:
            try:
                ts = float(ct)
                if ts > 1e12:
                    ts /= 1000
                data_time = datetime.fromtimestamp(ts, KST).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                data_time = str(ct)
        stale_min = None
        if data_time:
            try:
                stale_min = round((t0 - datetime.fromisoformat(data_time)).total_seconds() / 60, 1)
            except ValueError:
                pass

        if not values:
            print("::warning::/device/latest 에서 측정값을 받지 못해 발전소 요약값으로 대체했습니다.")
        status.update({"ok": True, "consecutive_failures": 0, "data_time": data_time,
                       "device_state": dev.get("device_state"), "device_points": len(values),
                       "data_age_min": stale_min, "logger_online": (stale_min is not None and stale_min < 20)})
        latest = {
            "updated_at": t0.isoformat(timespec="seconds"),
            "station": {"id": inv["station_id"], "name": inv.get("station_name")},
            "device": {"sn": inv["device_sn"], "type": inv.get("device_type"), "name": inv.get("device_name")},
            "status": status,
            "data": norm,
            "station_summary": station_summary,
            "units": dev.get("units", {}),
            "raw": values,          # API 가 준 모든 key 를 그대로 보존 (필드 확인용)
        }
        write_json(DATA / "latest.json", latest)
        append_history(t0, norm, status)
        update_daily(t0, norm)
        try:
            fill_note = fill_recent(client, inv["device_sn"], t0)
        except Exception as e:  # noqa: BLE001  보충이 실패해도 이번 수집은 성공
            fill_note = f"기록 보충 실패: {str(e)[:160]}"
        if CRYPTO is not None:
            # 공개 저장소의 Actions 로그는 누구나 볼 수 있으므로 측정값을 찍지 않는다
            print(f"[OK] {t0:%Y-%m-%d %H:%M} 수집 완료 (암호화 저장, 항목 {len(values)}개, data_age {stale_min}min) · {fill_note}")
        else:
            print(f"[OK] {t0:%Y-%m-%d %H:%M} PV {norm.get('pv_power_w')}W  SOC {norm.get('battery_soc_pct')}%  "
                  f"Load {norm.get('load_power_w')}W  data_age {stale_min}min  (암호화 꺼짐) · {fill_note}")
        return 0
    except Exception as e:  # noqa: BLE001
        status["ok"] = False
        status["error"] = str(e)[:500]
        status["consecutive_failures"] += 1
        prev = prev or {"data": {}, "raw": {}}
        prev["status"] = {**prev.get("status", {}), **status}
        prev["last_error_at"] = t0.isoformat(timespec="seconds")
        write_json(DATA / "latest.json", prev)
        print(f"[FAIL] {t0:%Y-%m-%d %H:%M} {e}", file=sys.stderr)
        print(f"::warning::Deye 수집 실패 ({status['consecutive_failures']}회 연속): {str(e)[:200]}")
        # 실패해도 status 가 기록된 latest.json 을 커밋해야 화면에 표시되므로 0 으로 끝낸다
        return 0


def append_history(t: datetime, norm: dict, status: dict) -> None:
    day = t.strftime("%Y-%m-%d")
    path = HIST / f"{day}.json"
    rows = read_json(path, [])
    row = {"t": t.strftime("%H:%M"), "dt": status.get("data_time")}
    row.update({k: norm.get(k) for k in HISTORY_FIELDS})
    # 로거 시각이 같은 행이 이미 있으면 (새 데이터가 아직 안 올라온 경우) 중복 저장하지 않는다
    if rows and row["dt"] and rows[-1].get("dt") == row["dt"]:
        return
    rows.append(row)
    write_json(path, rows)
    # 오래된 history 정리
    cutoff = (t - timedelta(days=HISTORY_KEEP_DAYS)).strftime("%Y-%m-%d")
    for p in HIST.glob("*.json"):
        if p.stem < cutoff:
            p.unlink()


def update_daily(t: datetime, norm: dict) -> None:
    """인버터가 주는 '오늘 누적값'을 날짜별로 기록한다 (하루의 마지막 값이 그날 집계)."""
    day = t.strftime("%Y-%m-%d")
    daily = read_json(DATA / "daily.json", {})
    entry = daily.get(day, {})
    for k in ("pv_today_kwh", "load_today_kwh", "charge_today_kwh", "discharge_today_kwh"):
        v = norm.get(k)
        if v is not None:
            entry[k] = max(v, entry.get(k) or 0)
    entry["last"] = t.strftime("%H:%M")
    daily[day] = entry
    cutoff = (t - timedelta(days=DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
    daily = {d: v for d, v in sorted(daily.items()) if d >= cutoff}
    write_json(DATA / "daily.json", daily)


# ───────────────────────── 과거 데이터 가져오기 (--backfill) ─────────────────────────
# Deye Cloud 에 남아 있는 과거 기록을 받아 history/ 와 daily.json 에 채운다.
#   1) 일 합계: /device/history granularity=2 (31일씩). 안 되면 /station/history granularity=2
#   2) 5분 곡선: /device/history granularity=1 (하루씩, HISTORY_FIELDS 에 해당하는 key 만 요청)
# 이미 있는 행과 시각이 겹치면(90초 이내) 건너뛴다. 270행 이상 찬 날은 --force 가 없으면 조회하지 않는다.
DAILY_KEYS = {   # 일 합계 응답 key → daily.json 항목
    "pv_today_kwh": ["Production", "generationValue"],
    "load_today_kwh": ["Consumption", "consumptionValue"],
    "charge_today_kwh": ["ChargingCapacity", "chargeValue"],
    "discharge_today_kwh": ["DischargingCapacity", "dischargeValue"],
}
FULL_DAY_ROWS = 270


def history_points() -> list[str]:
    pts = []
    for f in HISTORY_FIELDS:
        for k in FIELD_MAP.get(f, []):
            if k not in pts:
                pts.append(k)
    return pts


def _entries(res: dict) -> list:
    data = res.get("data")
    if isinstance(data, list):
        return data
    data = data if isinstance(data, dict) else res
    for k in ("dataList", "stationDataItems", "items", "list", "historyList"):
        v = data.get(k)
        if isinstance(v, list):
            return v
    return []


def _entry_values(e: dict) -> dict:
    items = e.get("itemList")
    if not isinstance(items, list):
        items = e.get("dataList") if isinstance(e.get("dataList"), list) else None
    if items is not None:
        return {(i.get("key") or i.get("name")): i.get("value") for i in items
                if isinstance(i, dict) and (i.get("key") or i.get("name"))}
    return {k: v for k, v in e.items() if not isinstance(v, (list, dict))}


def _entry_time(e: dict):
    """응답 한 항목의 시각 → KST datetime (날짜만 있으면 그날 00:00). 못 읽으면 None."""
    if e.get("year") and e.get("month") and e.get("day"):
        try:
            return datetime(int(e["year"]), int(e["month"]), int(e["day"]), tzinfo=KST)
        except (TypeError, ValueError):
            return None
    for k in ("time", "collectionTime", "timeStamp", "timestamp", "date"):
        v = e.get(k)
        if v in (None, ""):
            continue
        try:
            ts = float(v)
            if ts > 1e12:
                ts /= 1000
            return datetime.fromtimestamp(ts, KST)
        except (TypeError, ValueError):
            pass
        s = str(v).strip().replace("/", "-")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:19], fmt).replace(tzinfo=KST)   # 시간대 표기가 없으면 발전소 시간(KST)으로 본다
            except ValueError:
                continue
    return None


def _shape(res: dict) -> str:
    """파싱이 안 될 때 로그에 남길 응답 구조 (값은 빼고 key 이름만)."""
    data = res.get("data") if isinstance(res.get("data"), (dict, list)) else res
    top = sorted(k for k in res if k not in ENVELOPE)
    inner = sorted(data) if isinstance(data, dict) else f"list[{len(data)}]"
    ents = _entries(res)
    first = sorted(ents[0]) if ents and isinstance(ents[0], dict) else None
    keys = sorted(_entry_values(ents[0]))[:20] if ents and isinstance(ents[0], dict) else None
    return f"top={top} data={inner} 항목수={len(ents)} 첫항목key={first} 값key={keys}"


def _daily_from(res: dict) -> dict[str, dict]:
    out = {}
    for e in _entries(res):
        if not isinstance(e, dict):
            continue
        t = _entry_time(e)
        if t is None:
            continue
        vals = _entry_values(e)
        rec = {}
        for name, keys in DAILY_KEYS.items():
            for k in keys:
                if k in vals and to_num(vals[k]) is not None:
                    rec[name] = to_num(vals[k])
                    break
        if rec:
            out[t.strftime("%Y-%m-%d")] = rec
    return out


def _frames_from(res: dict, day: str) -> list[dict]:
    rows = []
    for e in _entries(res):
        if not isinstance(e, dict):
            continue
        t = _entry_time(e)
        if t is None or t.strftime("%Y-%m-%d") != day:
            continue
        norm = normalize(_entry_values(e))
        row = {"t": t.strftime("%H:%M"), "dt": t.isoformat(timespec="seconds")}
        row.update({k: norm.get(k) for k in HISTORY_FIELDS})
        row["src"] = "api"
        if any(row[k] is not None for k in HISTORY_FIELDS):
            rows.append(row)
    return rows


def _epoch(row: dict):
    try:
        return datetime.fromisoformat(row["dt"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def merge_rows(old: list[dict], new: list[dict]) -> tuple[list[dict], int]:
    have = [x for x in (_epoch(r) for r in old) if x is not None]
    added = []
    for r in new:
        e = _epoch(r)
        if e is None or any(abs(e - h) < 90 for h in have):
            continue
        have.append(e)
        added.append(r)
    if not added:
        return old, 0
    rows = old + added
    rows.sort(key=lambda r: r.get("dt") or "")
    return rows, len(added)


def fetch_frames(client: "DeyeClient", sn: str, day: str, points: list[str], log=print, split: bool = True) -> list[dict]:
    """/device/history granularity=1 로 하루치 5분 기록을 받아 history 행으로 바꾼다.
    한 번에 거절되면 4개씩 나눠 다시 받는다. 응답에서 행을 못 읽으면 구조(key 이름만)를 남긴다."""
    try:
        res = client.device_history_frames(sn, day, points)
        new = _frames_from(res, day)
        if not new and _entries(res):
            log(f"   {day}: 응답은 있으나 읽은 행 0 · 응답 구조 {_shape(res)}")
        return new
    except ApiError as e:
        if not split:
            log(f"   {day}: 조회 실패 ({str(e)[:120]})")
            return []
        log(f"   {day}: 한 번에 조회 실패 ({str(e)[:120]}) → 나눠서 재시도")
    by_dt: dict[str, dict] = {}
    for j in range(0, len(points), 4):
        try:
            part = client.device_history_frames(sn, day, points[j:j + 4])
        except ApiError as e2:
            log(f"      {points[j:j + 4]} 실패: {str(e2)[:120]}")
            continue
        for e3 in _entries(part):
            if isinstance(e3, dict) and _entry_time(e3) is not None:
                k = _entry_time(e3).isoformat()
                by_dt.setdefault(k, {"time": _entry_time(e3).timestamp(), "itemList": []})
                by_dt[k]["itemList"] += [{"key": kk, "value": vv} for kk, vv in _entry_values(e3).items()]
    return _frames_from({"dataList": list(by_dt.values())}, day)


def day_totals(rows: list[dict]) -> dict:
    """행에 남은 '오늘 누적' 최대값 = 그날 합계"""
    rec = {}
    for k in ("pv_today_kwh", "load_today_kwh", "charge_today_kwh", "discharge_today_kwh"):
        vals = [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]
        if vals:
            rec[k] = max(vals)
    return rec


def fill_recent(client: "DeyeClient", sn: str, t: datetime) -> str:
    """GitHub 예약 실행은 5분을 지키지 않는다 (새 저장소는 30분 이상 밀리기도 함).
    그래서 매 수집마다 Deye 에 남아 있는 오늘 5분 기록을 받아 빈 시각을 채운다.
    새벽 6시 전에는 어제 기록이 덜 찼으면 어제도 한 번 채운다. 실패해도 수집은 성공으로 둔다."""
    points = history_points()
    today = t.strftime("%Y-%m-%d")
    days = [today]
    yday = (t - timedelta(days=1)).strftime("%Y-%m-%d")
    if t.hour < 6 and len(read_json(HIST / f"{yday}.json", [])) < FULL_DAY_ROWS:
        days.append(yday)
    notes = []
    for day in days:
        path = HIST / f"{day}.json"
        quiet: list[str] = []
        new = fetch_frames(client, sn, day, points, log=quiet.append, split=False)   # 매 수집이라 호출은 1번만
        old = read_json(path, [])
        rows, added = merge_rows(old, new)
        if added:
            write_json(path, rows)
            if day != today:
                daily = read_json(DATA / "daily.json", {})
                entry = dict(daily.get(day, {}))
                for k, v in day_totals(rows).items():
                    entry[k] = max(v, entry.get(k) or 0)
                daily[day] = entry
                write_json(DATA / "daily.json", dict(sorted(daily.items())))
        notes.append(f"{'오늘' if day == today else '어제'} +{added}행(합계 {len(rows)})")
        if quiet:
            notes.append("기록 조회 문제: " + quiet[0].strip()[:160])
    return " · ".join(notes)


def _parse_day(s: str, default: datetime) -> datetime:
    s = (s or "").strip()
    if not s:
        return default
    try:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        raise ApiError(f"날짜 형식은 YYYY-MM-DD 입니다: {s!r}")


def backfill(start_s: str, end_s: str, force: bool) -> int:
    try:
        init_crypto()
        today = now_kst().replace(hour=0, minute=0, second=0, microsecond=0)
        start = _parse_day(start_s, today - timedelta(days=30))
        end = min(_parse_day(end_s, today), today)
        if start > end:
            raise ApiError(f"시작일 {start:%Y-%m-%d} 이 종료일 {end:%Y-%m-%d} 보다 늦습니다.")
        client = DeyeClient()
        inv = resolve_inventory(client)
    except ApiError as e:
        print(f"::error::{e}")
        return 1
    sn, sid = inv["device_sn"], inv["station_id"]
    days = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range((end - start).days + 1)]
    print(f"과거 데이터 가져오기: {days[0]} ~ {days[-1]} ({len(days)}일)")

    # 1) 일 합계
    api_daily: dict[str, dict] = {}
    for i in range(0, len(days), 31):
        a, b = days[i], days[min(i + 30, len(days) - 1)]
        got = {}
        try:
            res = client.device_history_daily(sn, a, b)
            got = _daily_from(res)
            if not got:
                print(f"   일 합계(장비) {a}~{b}: 0일 · 응답 구조 {_shape(res)}")
        except ApiError as e:
            print(f"   일 합계(장비) {a}~{b} 실패: {e}")
        if len(got) < (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days + 1:
            try:
                res = client.station_history_daily(sid, a, b)
                st = _daily_from(res)
                for d, rec in st.items():
                    got.setdefault(d, rec)
                if not st:
                    print(f"   일 합계(발전소) {a}~{b}: 0일 · 응답 구조 {_shape(res)}")
            except ApiError as e:
                print(f"   일 합계(발전소) {a}~{b} 실패: {e}")
        api_daily.update(got)
        print(f"   일 합계 {a}~{b}: {len(got)}일")

    # 2) 5분 곡선
    points = history_points()
    total_added, filled_days, empty_days = 0, 0, []
    row_daily: dict[str, dict] = {}
    for day in days:
        path = HIST / f"{day}.json"
        old = read_json(path, [])
        if len(old) >= FULL_DAY_ROWS and not force:
            print(f"   {day}: 이미 {len(old)}행 → 건너뜀")
            rows = old
        else:
            new = fetch_frames(client, sn, day, points)
            rows, added = merge_rows(old, new)
            if added:
                write_json(path, rows)
                total_added += added
                filled_days += 1
            if not rows:
                empty_days.append(day)
            print(f"   {day}: 받은 {len(new)}행 · 추가 {added}행 · 합계 {len(rows)}행")
        rec = day_totals(rows)   # API 일 합계가 없을 때 대체
        if rec:
            row_daily[day] = rec

    # 3) daily.json 병합: 지난 날은 API 일 합계 우선, 오늘은 기존처럼 최대값 유지
    daily = read_json(DATA / "daily.json", {})
    today_s = today.strftime("%Y-%m-%d")
    touched = 0
    for day in days:
        src = api_daily.get(day) or row_daily.get(day)
        if not src:
            continue
        entry = dict(daily.get(day, {}))
        for k, v in src.items():
            entry[k] = max(v, entry.get(k) or 0) if day == today_s else v
        entry.setdefault("last", None)
        if day != today_s:
            entry["src"] = "api" if day in api_daily else "rows"
        if entry != daily.get(day):
            daily[day] = entry
            touched += 1
    if touched:
        cutoff = (today - timedelta(days=DAILY_KEEP_DAYS)).strftime("%Y-%m-%d")
        write_json(DATA / "daily.json", {d: v for d, v in sorted(daily.items()) if d >= cutoff})
    print(f"[OK] 완료: 5분 데이터 {filled_days}일에 {total_added}행 추가 · 일 합계 {touched}일 갱신"
          + (f" · 데이터 없는 날 {len(empty_days)}일 ({empty_days[0]} 등)" if empty_days else ""))
    return 0


# ───────────────────────── 설정 확인 모드 ─────────────────────────
def check() -> int:
    init_crypto()
    client = DeyeClient()
    client.obtain_token()
    print("토큰 발급 OK (서버:", client.base + ")")
    stations = client.stations_with_devices()
    if not stations:
        print("발전소가 없습니다. Deye Cloud 앱에서 발전소를 만들고 로거를 추가하세요.")
        return 1
    for s in stations:
        print(f"\n발전소 id={s.get('id') or s.get('stationId')}  name={s.get('name')}")
        for d in s.get("deviceListItems") or []:
            print(f"   장비 sn={d.get('deviceSn')}  type={d.get('deviceType')}  name={d.get('name') or d.get('deviceName')}")
    print("\n발전소가 여럿이면 DEYE_STATION_ID, 인버터를 지정하려면 DEYE_DEVICE_SN 을 Secrets 에 추가하세요.")
    return 0


def points() -> int:
    init_crypto()
    client = DeyeClient()
    inv = resolve_inventory(client)
    pts = client.measure_points(inv["device_sn"], inv.get("device_type") or "INVERTER")
    print(f"장비 {inv['device_sn']} measurePoints ({len(pts)}개):")
    for p in pts:
        print("  ", p)
    dev = client.device_latest([inv["device_sn"]]).get(inv["device_sn"], {})
    print("\n/device/latest 현재값:")
    for k, v in dev.get("values", {}).items():
        print(f"   {k} = {v} {dev.get('units', {}).get(k, '')}")
    return 0


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--check":
        sys.exit(check())
    if arg == "--points":
        sys.exit(points())
    if arg == "--backfill":
        rest = [a for a in sys.argv[2:] if a != "--force"]
        sys.exit(backfill(rest[0] if rest else "", rest[1] if len(rest) > 1 else "", "--force" in sys.argv))
    sys.exit(collect())
