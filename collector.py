#!/usr/bin/env python3
"""
Deye Cloud OpenAPI 수집기 — 5kW 목업 모니터링

하는 일 (한 번 실행에 한 사이클):
  1. 토큰 확보: 캐시 파일(.cache/token.json)이 유효하면 재사용, 아니면 재발급
  2. 발전소·장비 목록 확보: 캐시(.cache/inventory.json)가 있으면 재사용
  3. 발전소 최신값(/station/latest) + 인버터 최신값(/device/latest) 조회
  4. data/latest.json 덮어쓰기
  5. data/history/YYYY-MM-DD.json 에 한 행 추가 (KST 기준 날짜)
  6. data/daily.json 일간 집계 갱신 (최근 90일)
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
HISTORY_KEEP_DAYS = 400     # history 파일 보관 일수
DAILY_KEEP_DAYS = 90        # daily.json 보관 일수

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
# 파일 형식: {"enc": "aesgcm-pbkdf2-v1", "iv": b64(12바이트), "ct": b64(암호문+태그)}
# AAD(추가 인증 데이터) = 저장소 기준 상대 경로 (예: "data/latest.json") → 파일을 바꿔치기하면 복호화 실패
# 키 정보: data/key.json = {"v":1, "kdf":"PBKDF2-SHA256", "iter":600000, "salt":b64, "check":<봉투>}
#   salt 와 iter 는 공개되어도 된다. check 는 브라우저가 비밀번호를 빠르게 검증하는 용도.
KEY_FILE = DATA / "key.json"
KDF_ITER = 600_000
ENC_TAG = "aesgcm-pbkdf2-v1"
CHECK_TEXT = "deye-monitor-ok"
CRYPTO = None   # 암호화 사용 시 Crypto 인스턴스


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _is_env(obj) -> bool:
    return isinstance(obj, dict) and obj.get("enc") == ENC_TAG


def _aad(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


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
        self.rotated = False
        if "check" in meta:
            if self._try(meta["check"], "check", self.key) == CHECK_TEXT:
                pass
            elif self.old_key and self._try(meta["check"], "check", self.old_key) == CHECK_TEXT:
                self.rotated = True
            else:
                raise ApiError("DATA_PASSPHRASE 가 기존 데이터와 맞지 않습니다. 비밀번호를 바꾼 경우 "
                               "DATA_PASSPHRASE_OLD 에 이전 비밀번호를 넣고 한 번 실행하세요.")
        meta["check"] = self._enc_raw(CHECK_TEXT.encode(), "check", self.key)
        DATA.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(json.dumps(meta, indent=1), encoding="utf-8")
        self._migrate()

    def _derive(self, pw: str) -> bytes:
        pw = unicodedata.normalize("NFC", pw)
        kdf = self._PBKDF2(algorithm=self._hashes.SHA256(), length=32, salt=self.salt, iterations=self.iter)
        return kdf.derive(pw.encode("utf-8"))

    def _enc_raw(self, data: bytes, aad: str, key: bytes) -> dict:
        iv = secrets.token_bytes(12)
        ct = self._AESGCM(key).encrypt(iv, data, aad.encode("utf-8"))
        return {"enc": ENC_TAG, "iv": _b64e(iv), "ct": _b64e(ct)}

    def _try(self, env: dict, aad: str, key: bytes):
        try:
            return self._AESGCM(key).decrypt(_b64d(env["iv"]), _b64d(env["ct"]), aad.encode("utf-8")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None

    def encrypt(self, obj, aad: str) -> dict:
        return self._enc_raw(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), aad, self.key)

    def decrypt(self, env: dict, aad: str):
        txt = self._try(env, aad, self.key)
        if txt is None and self.old_key:
            txt = self._try(env, aad, self.old_key)
        if txt is None:
            raise ApiError(f"복호화 실패: {aad} (비밀번호가 다르거나 파일이 손상됨)")
        return json.loads(txt)

    def _migrate(self) -> None:
        """평문으로 남은 파일, 또는 이전 비밀번호로 암호화된 파일을 현재 키로 다시 쓴다."""
        targets = [p for p in list(DATA.rglob("*.json")) + list(CACHE.glob("*.json")) if p != KEY_FILE]
        for p in targets:
            try:
                obj = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            aad = _aad(p)
            if _is_env(obj):
                if self._try(obj, aad, self.key) is not None:
                    continue
                plain = self.decrypt(obj, aad)       # old_key 로 풀림 (안 풀리면 예외)
            else:
                plain = obj                          # 평문 → 암호화
            p.write_text(json.dumps(self.encrypt(plain, aad)), encoding="utf-8")


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
        return CRYPTO.decrypt(obj, _aad(path))
    return obj


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if CRYPTO is not None and path != KEY_FILE:
        text = json.dumps(CRYPTO.encrypt(obj, _aad(path)))
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
        if CRYPTO is not None:
            # 공개 저장소의 Actions 로그는 누구나 볼 수 있으므로 측정값을 찍지 않는다
            print(f"[OK] {t0:%Y-%m-%d %H:%M} 수집 완료 (암호화 저장, 항목 {len(values)}개, data_age {stale_min}min)")
        else:
            print(f"[OK] {t0:%Y-%m-%d %H:%M} PV {norm.get('pv_power_w')}W  SOC {norm.get('battery_soc_pct')}%  "
                  f"Load {norm.get('load_power_w')}W  data_age {stale_min}min  (암호화 꺼짐)")
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
    sys.exit(collect())
