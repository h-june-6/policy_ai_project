# AI 법·정책 동향 분석 플랫폼

## 1. 사전 준비

### PostgreSQL

서버가 떠 있어야 한다. 데이터베이스는 **직접 만들 필요 없다** — 첫 실행 때
`postgres` DB에 붙어 없으면 만든다.

전용 계정을 쓰는 것을 권장한다(운영 시 필수).


### 파이썬 패키지

`requirements.txt`에 필요한 것이 전부 들어 있다. 

AI 분석을 쓸 거면 `openai`를 따로 깐다(선택 — `llm.enabled=false`면 필요 없다).

형식별로 필요한 패키지가 갈리고, **하나만 빠지면 그 형식만 조용히 실패한다.**

| 없으면 안 되는 것 | 빠졌을 때 |
|---|---|
| `python-multipart` | 서버는 뜨지만 `/api/check-document`가 500 |
| `python-docx` / `pdfplumber` / `pyhwp`+`six` | 그 확장자만 못 읽는다 (`.txt`만 쓸 거면 불필요) |
| `openpyxl` | 엑셀 양식 업로드·양식 내려받기 불가 |
| `reportlab` / `python-hwpx` | PDF / 한글 다운로드만 불가 (마크다운은 항상 된다) |

> `python-docx`는 이름이 비슷한 `docx`(파이썬 2용)와 다른 패키지다. 그쪽이 깔려 있으면
> `import docx`는 되면서 `No module named exceptions`로 죽는다.
> `pyhwp`는 `six`를 런타임에 쓰는데 의존성에 걸어 두지 않아 따로 깔아야 한다.

> PDF 글꼴은 `POLICY_AI_PDF_FONT` → OS 기본 한글 글꼴 → 저장소에 동봉한
> `static/fonts/NotoSansKR-Regular.ttf` 순으로 찾는다(`report.py`의 `_FONT_CANDIDATES`).
> 동봉본이 있어 글꼴이 없는 환경에서도 PDF가 나온다.

### 법제처 인증키

[open.law.go.kr](https://open.law.go.kr) 가입 후 발급받아 `config.json`의 `law_api_key`에 넣는다.

---

## 2. 설정 — `config.json`

```json
{
  "law_api_key": "발급받은키",
  "llm": {
    "enabled": true,
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "base_url": "",
    "max_input_chars": 20000,
    "max_tokens": 2500
  },
  "db": {
    "host": "127.0.0.1", "port": 5432,
    "user": "postgres", "password": "", "name": "policy_ai"
  },
  "collect": { "max_analyze": 20 },
  "auto": { "init_on_startup": true, "daily_check": true, "daily_time": "22:00" }
}
```

| 키 | 의미 |
|---|---|
| `llm.enabled` | `false`면 AI를 쓰지 않고 규칙 기반으로만 판정 (과금 없음) |
| `llm.base_url` | Qwen 등 OpenAI 호환 서버 주소. 비우면 OpenAI |
| `llm.max_input_chars` | 조문 입력 상한(기본 20000). 넘으면 앞에서 자르지 않고 **조문 단위로 선별**하며, 분석 범위가 `선별 분석`으로 표시된다 |
| `llm.max_tokens` | 응답 토큰 상한 |
| `collect.max_analyze` | 자동 분석 1회당 최대 건수 (과금 상한) |
| `auto.init_on_startup` | 서버 시작 시 미적재분 자동 수집 |
| `auto.daily_check` | 매일 개정 확인 자동 실행 |
| `auto.daily_time` | 실행 시각 `HH:MM` (기본 `22:00`) |

`auto.daily_time`은 **설정** 탭에서도 바꿀 수 있다. 화면에서 바꾸면 다음 회차부터 반영된다
(스케줄러가 매 회차마다 설정을 다시 읽는다).

### 비밀값은 환경변수로 뺄 수 있다

설정 파일에 키를 남기고 싶지 않으면 환경변수가 우선한다.


---


### 첫 실행 때 일어나는 일

```
① 데이터베이스 policy_ai 자동 생성
② 테이블 생성
③ 감시 법령 107건 자동 등록
④ 기본 부서 생성 + 감시 법령 전부 귀속 → 전사 관리자 계정 생성
⑤ 1초 뒤 전문 자동 수집 시작 (법제처 API 200회 남짓, 수 분 소요)
⑥ 오늘 점검 이력이 없으므로 판본 대조 1회 실행
⑦ 이후 매일 auto.daily_time(기본 22:00)에 개정 확인 대기
```

수집이 끝날 때까지 화면의 통계는 비어 있다. 진행 상황은 아래 4번을 참고.

### 로그인 — 첫 관리자 계정

**화면은 로그인해야 열린다.** 로그인 가능한 전사 관리자가 하나도 없으면 서버가
시작할 때 하나 만든다.

| 환경변수 | 없을 때 |
|---|---|
| `POLICY_AI_ADMIN_EMAIL` | `admin@example.com` |
| `POLICY_AI_ADMIN_PASSWORD` | 임의 생성 후 **기동 로그에 한 번만** 찍힌다 |


비밀번호를 주지 않았다면 기동 로그의 `임시 비밀번호:` 줄을 놓치지 말 것 — 다시 표시되지
않는다. 로그인한 뒤 화면에서 바꾸면 된다.

계정은 **전사 관리자 / 부서 관리자 / 일반**의 세 등급이고, 이후 계정 추가는 화면의
**계정 관리** 탭에서 한다. 전사 관리자가 삭제·정지되어 아무도 관리에 못 들어가면
다음 기동 때 위 규칙으로 다시 만들어진다.

---
