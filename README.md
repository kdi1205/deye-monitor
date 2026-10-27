# 5kW 목업 모니터링 (Deye Cloud → GitHub Actions → GitHub Pages)

인버터 내장 로거가 Deye Cloud에 올린 데이터를 5분마다 OpenAPI로 받아 JSON으로 저장하고,
GitHub Pages가 index.html과 함께 서비스한다. 서버·하드웨어 없음.

## 파일

| 파일 | 역할 |
|---|---|
| collector.py | 수집기. 토큰 발급, 발전소·장비 조회, 최신값 암호화 저장. 암호화에 cryptography 사용 |
| .github/workflows/collect.yml | 5분 cron → 수집 → data/ 커밋 → Pages 배포 |
| index.html | 모니터링 화면. data/*.json 을 fetch해 카드·그래프 표시 |
| data/latest.json | 현재값 한 묶음 (정규화 data + 원본 raw + status) |
| data/history/YYYY-MM-DD.json | 하루치 5분 간격 배열 (KST) |
| data/daily.json | 일간 발전·소비·충방전량 (최근 90일) |

## 데이터 암호화 (DATA_PASSPHRASE)

- 수집기는 data/ 의 모든 JSON 과 .cache/ 의 토큰을 AES-256-GCM 으로 암호화해 저장한다.
  키는 비밀번호에서 PBKDF2-SHA256(60만 회)으로 만든다. data/key.json 에는 salt 와 검증용 값만 있다.
- 화면(index.html)은 처음 열 때 비밀번호를 묻고, 브라우저 안에서 복호화한다. "기억"을 켜면 그 브라우저에 키를 저장하고, 상단 "잠그기"로 지운다.
- 공개 저장소이므로 Actions 로그에는 측정값을 출력하지 않는다.
- DEYE_REQUIRE_ENCRYPTION=1 (워크플로우에 설정됨) 이라 DATA_PASSPHRASE 가 없으면 평문으로 쓰지 않고 실패한다.
- 비밀번호는 무작위 16자 이상을 쓴다. key.json 이 공개라 약한 비밀번호는 오프라인 대입 공격에 뚫릴 수 있다.
- 비밀번호 교체: Secrets 에 DATA_PASSPHRASE(새 값)와 DATA_PASSPHRASE_OLD(이전 값)를 넣고 한 번 실행 → 기존 파일이 모두 새 비밀번호로 재암호화된다. 그다음 DATA_PASSPHRASE_OLD 를 삭제한다.
- 공개로 남는 것: 코드, 파일 크기, 커밋 시각(=수집 시각). 측정값·발전소 이름·SN 은 암호문 안에 있다.

## 설정 순서 (한 번만)

1. 전제: Deye Cloud 앱에서 로거가 발전소에 추가되어 데이터가 보이는 상태여야 한다.
   ("not imported" 오류는 cloudservice@deye.com.cn 에 로거 SN 등록을 요청해 해결)
2. https://developer.deyecloud.com 에 Deye Cloud 계정으로 로그인 → Application 생성
   - 권한: Station and Device Monitoring 만 켠다 (Commission Control 은 끈다)
   - AppId, AppSecret 을 복사해 둔다
3. GitHub 에 공개 저장소를 만들고 이 폴더 내용을 올린다.
   - 공개여야 하는 이유: 무료 계정에서 Pages 는 공개 저장소만 되고, Actions 분 제한도 공개 저장소에는 없다.
4. 저장소 Settings → Secrets and variables → Actions
   - Secrets (5개): DEYE_APP_ID, DEYE_APP_SECRET, DEYE_EMAIL, DEYE_PASSWORD (Deye Cloud 로그인 비밀번호 그대로. 해시는 코드가 한다),
     DATA_PASSPHRASE (화면 비밀번호. 무작위 16자 이상)
   - Variables (선택): DEYE_REGION = eu (한국은 eu1 서버), DEYE_STATION_ID, DEYE_DEVICE_SN (발전소나 인버터가 여럿일 때만)
5. Settings → Pages → Build and deployment → Source: GitHub Actions
6. Actions 탭 → "Deye 수집 및 Pages 배포" → Run workflow 로 한 번 수동 실행
   - 로그에 `[OK] ... 수집 완료 (암호화 저장 ...)` 가 찍히면 성공
   - 2~3분 뒤 https://<계정>.github.io/<저장소>/ 에서 화면 확인
7. 이후는 5분마다 자동. 중단하려면 Actions 탭에서 워크플로우를 Disable.

## 처음 확인할 때 (로컬 PC 에서)

```bash
export DEYE_APP_ID=... DEYE_APP_SECRET=... DEYE_EMAIL=... DEYE_PASSWORD=...
python3 collector.py --check     # 토큰 발급 + 발전소/장비 목록
python3 collector.py --points    # 인버터가 주는 측정 항목(key) 전체와 현재값
python3 collector.py             # 수집 1회 → data/ 생성
```

로컬에서 암호화까지 시험하려면 `python3 -m pip install --user cryptography` 후 `export DATA_PASSPHRASE=...` 를 추가한다.

`--points` 결과에 collector.py 의 FIELD_MAP 에 없는 key 가 있으면, 그 key 를 FIELD_MAP 에 추가하면
화면 카드에 바로 연결된다. 매핑이 안 된 key 도 latest.json 의 raw 에는 전부 남는다.

## 동작 세부

- 토큰: .cache/token.json 에 보관. Actions 에서는 날짜 키 캐시로 복원하므로 토큰 발급은 하루 1회.
- 실패 처리: API 가 실패해도 이전 latest.json 을 유지하고 status.error 와 consecutive_failures 를 기록한다. 화면 상단에 빨간 경고로 표시된다.
- 로거 오프라인 판정: 로거 데이터 시각이 20분 이상 오래되면 "로거 데이터 n분 전" 으로 표시.
- history 중복 방지: 로거 데이터 시각이 직전 행과 같으면 (새 업로드가 아직 없으면) 행을 추가하지 않는다.
- 보관: history 400일, daily 90일. 하루 약 60KB.
- 커밋: 값이 바뀐 경우에만 커밋. 하루 최대 288 커밋이 쌓이지만 diff 가 작아 저장소 크기는 거의 늘지 않는다.

## 한계

- 데이터 해상도는 로거 업로드 주기(3~5분)에 묶인다.
- Pages 는 공개 URL 이다. 비공개가 필요하면 VM(FastAPI + SQLite) 방식으로 옮긴다. collector.py 와 index.html 은 그대로 쓸 수 있다.
- 인버터 측정값은 운영 참고용이다. dMRV 계량 근거는 Eastron 계량기 경로를 유지한다.
