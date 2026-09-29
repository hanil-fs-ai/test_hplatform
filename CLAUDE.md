# hplatform 클라이언트 PoC (Streamlit) — CLAUDE.md

> 이 문서는 Claude Code 가 Streamlit 앱을 만들 때 읽는 프로젝트 지시서다.
> API 규격은 **서버 코드(`server/internal/httpapi/model.go` 외)를 직접 읽어 확인한 값**이다. 추측 없음.

## 1. 목적

hplatform 을 **외부 챗봇/업무 시스템이 쓰는 것과 똑같이** 호출하는 가짜 클라이언트.

- 관리자/사용자 콘솔에서 서비스에 지정한 **LLM · STT · TTS 설정이 실제 호출에 바로 반영되는지** 확인한다.
- LLM/STT/TTS API 가 정상 동작하는지 눈과 귀로 확인한다.
- 클라이언트가 가진 것은 **API 키 하나**뿐이다. 모델 설정 UI 는 없다(콘솔 영역).
- 심플하게. 회원가입/권한/DB/브랜딩 없음.

## 2. 기술 스택

- Streamlit (Python 3.11+), HTTP 는 `requests`, 실시간(STT/TTS WebSocket)은 `websocket-client`
- 실행: `streamlit run app.py`

```
app.py            # 사이드바(서버·키) + st.tabs
api_client.py     # Base URL/헤더/에러 처리 공통화, 엔드포인트별 함수
requirements.txt  # streamlit, requests, websocket-client
ws_client.py      # STT/TTS WebSocket 수신 스레드 + queue 헬퍼 (모드 B, TTS)
mic_stt.html      # 브라우저 마이크 → 허브 WS JS 컴포넌트 (STT 모드 A). app.py 가 읽어 url/key 를 주입
```

## 3. 공통 규격 (전 API 공통)

### 3.1 Base URL

```
http://{host}:{port}/api/v1
```

- 서버 기본 포트 **8080**(`conf/dev/config.yaml` `PORT`). 사이드바에서 host/port 입력, 기본 `localhost` / `8080`.
- **STT/LLM/TTS 는 모두 `/api/v1/model/...` 아래**다. (`/api/stt`, `/api/llm`, `/api/tts` 같은 경로는 없다.)
- HTTPS 배포면 스킴을 고를 수 있게(`http`/`https`) 한다.

### 3.2 인증

API 키(`hp_live_…` 형태, `hp_` 접두사)를 헤더로 보낸다. 둘 다 동작하나 **`X-API-Key` 를 기본**으로 쓴다.

```
X-API-Key: hp_live_xxxxxxxx
```

- `Authorization: Bearer hp_live_…` 도 허용된다(`hp_` 로 시작할 때만 API 키로 취급).
- 키는 `type="password"` 로 입력받고 `st.session_state` 에만 둔다. 로그·화면에 평문 노출 금지.
- 이 키는 **특정 서비스에 소속**된다. 서버는 키 → 서비스로 정책(허용 provider/model/voice)과 지정 모델을 찾는다.

### 3.3 에러 응답

모든 에러는 HTTP status + JSON `{"message": "..."}` 이다. (성공 본문과 스키마가 다르니 status 로 먼저 분기.)

| status | 의미 |
|---|---|
| 400 | 잘못된 요청 — 필수값 누락, 모르는 provider 라벨, 허용 목록 밖 model, 모르는 voice, `messages` 비어 있음 등 |
| 401 | 키 없음/틀림 |
| 403 | 이 **서비스의 허용 범위 밖**(provider/model/voice). 내려진(withheld) TTS 모델 |
| 404 | (STT async) 그 provider 에 그런 job id 없음 — provider 를 다르게 보낸 경우가 대부분 |
| 422 | 고른 엔진이 그 기능(async/stream/ws/VC)을 지원 안 함 |
| 429 | 업스트림 레이트리밋 |
| 502 | 엔진에 닿지 못함 |

에러 시 `st.error(f"{status} — {message}")` 로 그대로 보여 준다. 예외를 삼키지 말 것.
그리고 **응답이 JSON 이 아닐 수 있으니**(프록시 HTML 등) `resp.json()` 실패 시 `resp.text` 로 폴백.

### 3.4 "설정이 바로 반영되는지" 확인의 핵심 규칙 ⭐

**`provider`/`model` 을 비워서 보내면 서버가 그 서비스에 지정된 값을 쓴다.**
우선순위: **요청에 명시한 값 > 서비스에 지정된 값 > 플랫폼 기본값**.

- LLM(`generate`)·STT(`transcribe` · `async` · `stream`)에 적용된다.
- provider 만 주고 model 을 비우면: 그 provider 가 서비스 지정값과 **같을 때만** 지정 model, 다르면 그 provider 의 기본 model.
- 지정값이 서비스 상한 밖이거나 꺼져 있으면 **조용히 기본값으로 폴백하지 않고 403/400** 이 난다.
- **TTS 는 이 규칙이 없다.** 서비스에 지정된 것은 모델이 아니라 **목소리 묶음(basket)** 이다 → §7.

따라서 UI 는 두 모드를 가진다:

- **「서비스 설정 그대로」(기본)**: provider/model 필드를 **아예 보내지 않는다**(빈 문자열도 보내지 말고 키 자체를 생략). → 콘솔에서 바꾼 설정이 반영되는지 확인하는 모드.
- **「직접 지정」**: `GET /model/providers` 결과로 드롭다운을 채워 명시적으로 보낸다.

LLM 은 응답의 `provider`/`model` 이 **실제로 쓰인 값**이라, 「서비스 설정 그대로」로 호출 후 이 값을 크게 표시하면 반영 여부가 바로 보인다.
STT 응답은 `provider` 만 온다(model 은 안 옴).

## 4. 사이드바 (공통)

1. 스킴 / Host / Port (기본 `http` / `localhost` / `8080`)
2. API 키 (password)
3. **[연결 확인] 버튼** — 아래 두 호출을 한 번에 하고 결과를 표시:
   - `GET /client/me` → `{"name", "tenant_id", "clearance", "scopes"}` (이 키가 어느 조직/권한인지)
   - `GET /model/providers` → §5. 결과를 `st.session_state["providers"]` 에 캐시해 각 탭 드롭다운에 쓴다.

## 5. 발견: `GET /model/providers`

키로 쓸 수 있는 엔진·모델을 알려 준다. **목록을 하드코딩하지 말고 이걸 먼저 부른다.**

응답(실제 필드):

```json
{
  "llm": ["deepseek", "claude"],
  "llm_default": "deepseek",
  "llm_models": {"deepseek": ["deepseek-chat"], "claude": ["..."]},
  "llm_prices": {"deepseek": {"deepseek-chat": {}}},
  "catalog": [{"provider_label": "deepseek", "model": "deepseek-chat", "display_name": "DeepSeek Chat", "is_default":
true}],
  "stt": ["hanil", "whisper"],
  "tts": ["hanulim-1"],
  "tts_models": {"hanulim-1": ["supertonic3"]},
  "tts_model_states": {"hanulim-1": [{"model": "supertonic3", "state": "ok"}]}
}
```

- 빈 배열 = 그 제품이 이 설치에 안 붙어 있다 → 해당 탭에 경고 표시하고 호출 버튼 비활성화.
- `llm_models[provider]` 가 유효한 model 값. **STT 는 model 목록이 응답에 없다** → STT model 은 자유 입력(선택, 비우면 서비스 지정/기본).
- `tts_model_states[].state`: `ok` 만 합성 가능. `withheld`(운영자가 내림)·`unavailable` 은 드롭다운에서 제외하거나 「사용 불가」 표시.
- 이 응답에 필드가 없거나 null 이어도 죽지 않게 `.get(key) or 기본값` 으로 읽는다.

## 6. 탭

`st.tabs(["STT", "LLM", "TTS"])`. 각 탭은 「입력 → 호출 버튼 → 결과」만. 요청 중 `st.spinner`.

### 6.1 LLM 탭

**`POST /model/generate`** · `Content-Type: application/json`

요청:

```json
{
  "messages": [{"role": "user", "content": "이 통화를 세 줄로 요약해줘"}],
  "system": "선택. 시스템 지시",
  "provider": "선택. 생략 시 서비스 지정값",
  "model": "선택",
  "temperature": 0.7,
  "max_tokens": 512,
  "stream": false
}
```

- ⚠️ 필드는 **`prompt` 가 아니라 `messages`** 배열(`{role, content}`)이다. 비면 400.
- `role` 은 `user` | `assistant` | `tool` 만 허용. **`system` 역할 메시지는 400** — 시스템 지시는 최상위 `system` 필드로 보낸다.
- `temperature`·`max_tokens`·`top_p`·`stop` 은 선택. 안 쓰면 **키를 생략**(0 이나 null 을 보내지 말 것 — 0 은 값으로 취급).
- 그 밖에 `tools`, `tool_choice`, `response_format`({"type":"text|json_object|json_schema", ...}) 이 있으나 **이 PoC 에서는 다루지 않는다**.
- 채팅형으로 `st.chat_message`/`st.chat_input` 을 쓰면 `st.session_state["messages"]` 에 user/assistant 를 누적해 통째로 보낸다(서버는 대화를 저장하지 않는다 — 매 턴 전체 이력을 클라이언트가 보낸다).

응답 (`stream: false`, 200):

```json
{
  "provider": "deepseek",
  "model": "deepseek-chat",
  "content": "답변 텍스트",
  "finish_reason": "stop",
  "usage": {"prompt_tokens": 35, "completion_tokens": 70, "total_tokens": 105}
}
```

- `provider`/`model` = **실제로 쓰인 값**. 크게 표시(반영 확인용). `tool_calls` 는 도구를 넘겼을 때만 온다.

**스트리밍 (`"stream": true`)** — SSE:

- `Content-Type: text/event-stream`. 각 프레임은 한 줄 `data: {json}` + 빈 줄.
- `requests.post(..., stream=True)` + `resp.iter_lines(decode_unicode=True)`. **`data: ` 접두사를 떼고 `json.loads`.** (그냥 JSON 으로 파싱하면 첫 줄부터 깨진다.) `[DONE]` 센티넬은 없다 — `done: true` 프레임으로 끝난다.
- 프레임 필드:

| 필드 | 설명 |
|---|---|
| `content_delta` | 이번에 추가된 글자. 이걸 이어 붙여 `st.write_stream` 에 yield |
| `model` | **첫 프레임에만** 옴 = 실제 실행 모델 |
| `finish_reason` | 마지막 부근 (`stop`, `length`, `error` 등) |
| `usage` | 마지막 부근 |
| `done` | `true` 면 종료 |
| `error` | 있으면 스트림 중 에러(HTTP status 는 이미 200 이라 **본문으로만** 알 수 있다) |
| `temperature_unsupported` | 첫 프레임. 이 모델이 temperature 를 거부해 빼고 재시도했다는 뜻 → 배지로 표시 |

- 스트림 시작 전 실패(400/403 등)는 일반 JSON 에러로 온다. `resp.status_code` 를 **먼저** 확인한 뒤 iter_lines.
- 스트림의 `provider` 는 프레임에 없다 → 서비스 지정 모드에서 provider 를 알고 싶으면 비스트리밍으로 확인하거나, 첫 프레임 `model` 만 표시.
- UI 는 「스트리밍」 체크박스로 전환. 기본 off(반영 확인이 목적이므로 provider 가 응답에 오는 비스트리밍이 낫다).

### 6.2 STT 탭

입력: `st.audio_input`(마이크) **또는** `st.file_uploader`(wav/mp3/m4a/flac/ogg/webm). 둘 다 지원.
옵션: language(BCP-47, 예 `ko`, 비우면 엔진 기본), provider/model(§3.4 두 모드).

> ⚠️ **이 PoC 는 동기 `POST /model/transcribe` 를 쓰지 않는다. 파일/마이크 입력은 전부 비동기(`/transcribe/async`)만.** 아래 multipart 규격은 async 제출에도 동일하다.

**`POST /model/transcribe/async`** · `multipart/form-data`

| 파트 | 필수 | 설명 |
|---|---|---|
| `audio` | ✅ | 오디오 파일. **파트 이름이 정확히 `audio`** |
| `provider` | | 엔진 라벨. 생략 시 서비스 지정값 |
| `model` | | 생략 시 서비스 지정값 |
| `language` | | BCP-47 |

```python
requests.post(url, headers=hdr,
    files={"audio": (name, data, mime)},         # 파일
    data={k: v for k, v in opts.items() if v})   # 빈 값은 보내지 않기
```

- `Content-Type` 은 직접 지정하지 말 것(requests 가 boundary 를 붙인다).
- 업로드 상한 **200MB**. 바로 응답을 기다리는 방식이라 **수십 초 이내 짧은 음성용**.
- `st.audio_input` 은 WAV 를 준다. 파일 mime 은 `uploaded.type` 사용.

응답 (200):

```json
{
  "provider": "whisper",
  "text": "요금 문의로 전화했습니다.",
  "language": "ko",
  "duration_ms": 4120,
  "segments": [{"start_ms": 0, "end_ms": 1900, "text": "요금 문의로", "confidence": 0.94}]
}
```

- `text` 를 크게, `provider` 표시(반영 확인), `segments` 는 표(`st.dataframe`)로, 원본 JSON 은 `st.expander`.
- `language`·`duration_ms`·`segments` 는 **없을 수 있음**(omitempty) — `.get` 으로.

**비동기 (긴 녹취)** — 탭 안 「긴 파일 (비동기)」 토글

1. `POST /model/transcribe/async` (요청은 위와 동일 multipart) → **202** `{"id": "...", "provider": "whisper"}`
2. `GET /model/transcribe/async/{id}?provider={submit 응답의 provider}` 를 **2~3초 간격** 폴링
   - **`provider` 쿼리는 제출에 쓴 것과 같은 값**이어야 한다. 제출 응답의 `provider` 를 그대로 저장해 폴링에 쓴다(안 그러면 404).
   - 응답 `{"status": "pending"|"done"|"failed", "provider": ..., "text": (done), "message": (failed)}`
   - `done` → `text` 표시, `failed` → `message` 를 `st.error`, `pending` → 계속. 최대 대기 시간(예 5분)을 두고 넘으면
중단.
3. 폴링은 `time.sleep` + `st.empty()` 갱신 루프로.

**실시간 (WebSocket)** — 탭 안 「실시간 (WebSocket)」 섹션 · `GET /model/transcribe/stream`

입력 모드가 둘이다. 라디오로 고른다.

| 모드 | 오디오가 흐르는 길 | 용도 |
|---|---|---|
| **A. 브라우저 마이크 (기본)** | 브라우저 JS → **허브에 직접 WS** | 실제 웹 클라이언트와 같은 경로 검증 |
| **B. 파일 재생** | 업로드 파일 → Python(서버)이 100ms씩 실시간 속도로 → 허브 | 같은 음성으로 반복·재현 테스트 |

`st.audio_input` 은 녹음이 끝난 뒤에야 파일을 주므로 실시간에 쓸 수 없다. 모드 A 는 **`st.components.v1.html` 안에 JS
를 넣어** 브라우저가 마이크를 잡고 허브에 직접 붙는다(Python 을 거치지 않음). 별도 패키지 불필요.

#### 모드 A — 브라우저 마이크 (JS 컴포넌트)

인증: 브라우저는 WS 에 헤더를 못 붙이므로 **서브프로토콜**로 보낸다(서버가 지원하는 정식 경로, 쿼리에 키를 넣지 않는다).

```js
const ws = new WebSocket(url, ['hplat.v1', 'hplat.token.' + apiKey]);
ws.binaryType = 'arraybuffer';
```

- `url` = `ws://{host}:{port}/api/v1/model/transcribe/stream?sample_rate=16000[&provider=..][&model=..]` (Python 이 만들어 HTML 문자열에 주입).
- API 키가 페이지 JS 에 노출된다 — 테스트 도구이고 사용자가 방금 입력한 키이므로 허용. 단 화면·콘솔 로그에 찍지 말 것.

마이크 캡처 (**AudioWorklet 권장**, ScriptProcessor 는 deprecated):

1. `navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}})`
2. `AudioContext` 를 만들고 `MediaStreamSource → AudioWorkletNode`. **브라우저 기본 표본율(보통 44.1/48kHz)로 캡처하고, 워크릿에서 16kHz 로 다운샘플**한다(`new AudioContext({sampleRate:16000})` 는 브라우저별로 불안정 — 의존하지 말 것).
3. 다운샘플한 float32 → **Int16 little-endian** 으로 변환해 `ws.send(int16.buffer)`. 약 100ms(1600 샘플=3200 바이트)씩.
4. 쿼리의 `sample_rate` 는 **실제로 보내는 표본율(16000)** 과 반드시 같아야 한다. 다르면 인식이 깨진다.

받는 프레임(`{text, final}`, 실패는 `{status:"failed", message}`)은 같은 컴포넌트 안에서 그려 준다: partial 은 회색 한 줄, final 은 확정 텍스트로 누적. 버튼: [시작] [중지].

제약 (문서에 그대로 표시):

- **`getUserMedia` 는 보안 컨텍스트에서만 동작한다**: `http://localhost` 또는 `https://`. Streamlit 을 다른 PC 에서 `http://192.168.x.x:8501` 로 열면 마이크가 막힌다 → HTTPS 로 띄우거나(`--server.sslCertFile/--server.sslKeyFile`) localhost 로 접속.
- **Streamlit 이 https 면 `ws://` 로 못 붙는다**(mixed content) → 허브도 `wss://` 여야 한다. 앱이 http/localhost 면 `ws://` 가능.
- `components.html` 은 iframe 이라 마이크 권한 프롬프트가 뜬다. iframe 에 `allow="microphone"` 이 필요한 환경이면 `components.html` 대신 `st.components.v1.declare_component` 로 로컬 컴포넌트를 만든다.
- 중지 시 `ws.close()` — **마지막 final 이 누락될 수 있으니**(아래) 중지 후 3초 대기하고 닫는다.
- 결과가 JS 안에서만 그려지므로 Python 쪽 `st.session_state` 로는 안 돌아온다(PoC 에서는 화면 표시로 충분). 지연·final 개수 같은 통계도 JS 에서 계산해 컴포넌트 안에 표시한다.

대안(모드 A 가 위 제약으로 안 될 때): `streamlit-webrtc` 로 서버가 오디오 프레임을 받아 Python 에서 §모드 B 와 같은 WS 로 넘긴다. WebRTC(STUN/UDP) 가 필요해 폐쇄망에서 까다롭고 의존성(`aiortc`, `av`)이 무겁다 → 기본은 JS 직접 연결.

#### 모드 B — 파일 재생 (서버 쪽 Python)

업로드한 파일을 Python 이 잘라 실시간 속도로 흘려보낸다. 서버가 받는 건 모드 A 와 같은 raw PCM 이므로 스트림 경로 검증 결과는 동일하다.

연결:

```
ws://{host}:{port}/api/v1/model/transcribe/stream?sample_rate=16000[&provider=..][&model=..]
X-API-Key: hp_live_…      # WS 핸드셰이크 헤더
```

- 쿼리: `provider`, `model`(생략 시 서비스 지정값 — §3.4), `sample_rate`(보내는 오디오의 표본율, Hz).
- ⚠️ **`language` 쿼리는 스트림에서 읽히지 않는다**(핸들러가 무시). 넣지 말 것.
- Python `websocket-client`: `websocket.create_connection(url, header=["X-API-Key: ..."])`.
  (`https` 배포면 `wss://`.)
- 핸드셰이크 전 실패는 WS 가 열리지 않고 **HTTP 상태**로 온다: 400(모르는 provider) · 401 · 403(허용 범위 밖) · 422(그 엔진이 스트리밍 미지원).

보내는 것: **binary 프레임 = raw PCM16LE, 모노, `sample_rate` Hz. WAV/mp3 헤더 없음.**

- 업로드 파일이 WAV 면 `wave` 모듈로 열어 PCM 을 꺼낸다. 16-bit 모노가 아니거나 표본율을 모르면 변환하거나 경고한다(mp3 등은 PoC 에서 미지원으로 안내해도 됨).
- 조각 크기 100ms 분량(16kHz → 3200 바이트)씩, **실시간 속도**(`time.sleep(0.1)`)로 보낸다.
- text 프레임은 서버가 읽지 않는다(무시). 종료 신호는 별도 메시지가 없다 — 소켓을 닫는 것뿐이다.

받는 것: JSON text 프레임

```json
{"text": "요금 문의로", "final": false}
{"text": "요금 문의로 전화했습니다.", "final": true}
{"status": "failed", "message": "사유"}
```

- `final: false` 는 계속 바뀌는 중간 결과(partial), `final: true` 는 확정 문장. `final` 은 false 일 때 필드 자체가 없을 수 있으니 `.get("final", False)`.
- `status: "failed"` 프레임이 오면 그 뒤로 끝이다 → `st.error(message)`.
- ⚠️ **소켓을 닫아도 마지막 final 이 나온다는 보장이 없다.** 오디오를 다 보낸 뒤 바로 닫지 말고, 결과가 **N초(예 3초) 동안 더 안 올 때까지** 받은 다음 닫는다. 화면에도 「마지막 확정 결과가 누락될 수 있음」을 표시.
- 구현: 별도 스레드가 수신해 `queue.Queue` 에 넣고, 메인 루프가 `st.empty()` 를 갱신해 partial 은 회색·final 은 확정 텍스트로 누적 표시. 보내는 쪽/받는 쪽은 서로 다른 스레드(송신 중에도 수신해야 한다). Streamlit 스크립트 스레드 밖에서 `st.*` 를 호출하지 말 것 — 스레드는 큐에만 넣는다.
- 연결 전 확인: WS 는 401 을 받고 재시도할 수 없다. 사이드바 「연결 확인」(`/client/me`)이 성공한 키인지 먼저 확인.
- 표시할 것: 첫 partial 까지의 지연, final 개수, 사용된 `provider`(스트림 응답 프레임에는 provider 가 없다 → 「서비스
설정 그대로」 모드에서 반영 여부는 비동기 `transcribe/async` 폴링 응답으로 확인).

### 6.3 TTS 탭

목소리는 **먼저 목록을 조회해서 고른다.** 임의 문자열을 넣으면 400/403.

**목소리 목록** (탭 진입 시 / 모델 바꿀 때):

- `GET /model/tts/voices?model={model}&provider={provider}` — 이 서비스가 **쓸 수 있는** 전부
  - `model` 은 필수(없으면 400). `provider` 는 선택(생략 시 기본 엔진).
  - 응답: `{"provider": "hanulim-1", "voices": [{"id": "F2", "display_name": "F2", "kind": "preset"|"clone", "tags": [], "state": "ok"}], "supports_registration": true}`
  - `state` 가 `ok` 가 아닌 항목은 「사용 불가」로 표시하고 선택 못 하게.
- `GET /model/tts/basket` — 콘솔에서 이 서비스에 **지정한 묶음**의 목소리
  - 응답: `{"basket": {"id","name","version"} | null, "voices": [{"provider","model","voice","display_name","tags","available","state"}]}`
  - `basket` 이 `null`/`voices` 가 `[]` 면 지정된 묶음이 없다는 뜻(에러 아님).
  - **「콘솔 설정 반영 확인」은 이쪽**이다: basket 목소리를 기본 선택지로 보여 주고, 「전체 보기」 토글로 `/tts/voices` 결과를 보여 준다. basket 항목은 `provider`/`model`/`voice` 를 그대로 합성 요청에 쓴다.

**`POST /model/synthesize`** · JSON

```json
{
  "text": "주문이 접수되었습니다.",
  "provider": "hanulim-1",
  "model": "supertonic3",
  "voice": "F2",
  "voice_mode": "preset",
  "language": "ko",
  "speed": 1.0
}
```

- 필수는 `text`. 나머지는 선택이지만 **목소리 목록에서 고른 `voice` 를 함께 보내는 것을 기본**으로 한다.
- `voice_mode`: 목소리 `kind` 와 맞춘다 — `preset` 목소리면 `"preset"`, `clone` 이면 `"clone"`. (clone 의 `voice` 는 플랫폼 id(UUID).)
- `speed` 는 재생 배율이 아니라 **말하기 속도**. 안 쓰면 키 생략(0 을 보내면 0 으로 취급될 수 있음).
- 그 외 `instructions`(톤 지시, 지원 모델만), `seed`, `format`, `output_check` 가 있으나 PoC 에서는 `instructions`·`seed` 정도만 「고급」 expander 에 둔다.
- 인라인 태그 `<pause=1.5>` 는 preset/clone 에서만 동작. `<sigh>` 류 감정 태그는 전 모델 미지원(조용히 제거됨).

응답 (200): **JSON 이 아니라 오디오 바이트**다.

- `Content-Type` 이 실제 형식(예 `audio/wav`, 실측 44100Hz 모노). `Content-Disposition` 에 파일명.
- `resp.content` 를 `st.audio(resp.content, format=resp.headers["Content-Type"])` 로 재생하고, `st.download_button` 도 붙인다.
- **JSON 파싱하지 말 것**(`resp.json()` 은 에러 응답일 때만).
- `output_check: true` 를 보냈을 때만 `X-Match-Rate`(되전사 일치율) 또는 `X-Output-Check: unavailable` 헤더가 온다 — 선택 체크박스로 제공하면 좋음.

**스트리밍 합성 (선택)** — `POST /model/synthesize/stream`

- 요청 필드는 `synthesize` 와 같다(추가로 `stream_mode` 등). 422 면 그 엔진이 스트리밍 미지원.
- 응답은 `audio/pcm` = **헤더 없는 raw PCM(s16le)**. WAV 가 아니다.
  샘플레이트는 응답 헤더 **`X-Sample-Rate`**, 모델은 `X-Model`.
- `st.audio` 로 재생하려면 직접 WAV 헤더를 붙인다:

```python
import io, wave
buf = io.BytesIO()
with wave.open(buf, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(int(resp.headers["X-Sample-Rate"]))
    w.writeframes(pcm)
```

- 스트림 도중 실패하면 HTTP 상태는 이미 200 이라 **연결이 끊긴 채 불완전한 응답**으로만 알 수 있다 → `requests.exceptions.ChunkedEncodingError` 를 잡아 표시.
- 첫 소리까지의 시간(TTFB)을 재서 함께 보여 주면 유용하다(`time.perf_counter()`).

**실시간 합성 (WebSocket)** — 탭 안 「실시간 (WebSocket)」 섹션 · `GET /model/synthesize/ws`

전체 규격: `server/docs/guide/tts-websocket-client-guide.md` (아래는 PoC 구현에 필요한 부분).

연결 (연결 설정은 **첫 프레임이 아니라 쿼리 파라미터**):

```
ws://{host}:{port}/api/v1/model/synthesize/ws?voice_mode=preset&voice=F1&language=ko[&model=..][&provider=..]
X-API-Key: hp_live_…
```

- `voice_mode` **필수**(없으면 400). 값: `preset` · `clone` · `custom_voice` · `voice_design` · `random_voice`. PoC 는 목소리 목록의 `kind` 에 맞춰 `preset`/`clone`.
- `voice`: 프리셋명 또는 클론 UUID. 목소리 목록에서 고른 값을 쓴다.
- `language`: **명시할 것** — supertonic3 은 WS 에서 `auto` 를 받지 않는다.
- 선택: `model`, `provider`, `speed`, `seed`, `stream_mode`(`buffered` 기본|`realtime`), `sample_rate`, `pcm_format`(`s16le` 기본), `instructions`.
- 핸드셰이크 전 실패는 **HTTP 상태**: 400(`voice_mode` 없음·모르는 model) · 401 · 403(목소리가 서비스 허용 범위 밖) ·
422(엔진이 WS 미지원).
- REST 합성과 달리 WS 는 provider 생략 시 기본 엔진, 서비스 지정 규칙 없음.

세션 흐름:

1. 연결되면 서버가 JSON 한 번: `{"type":"ready","sample_rate":44100,"pcm_format":"s16le"}` (`welcome` 없음). **이 `sample_rate` 를 저장**한다.
2. 클라이언트 → 서버 (JSON text):
   - `{"type":"text","data":"문장"}` (여러 번 가능)
   - `{"type":"flush","final":true}` — **발화 끝. 반드시 이걸로 끝낼 것.**
   - `{"type":"cancel"}` — 끼어들기(barge-in). 서버의 남은 합성을 멈춘다.
   - `{"type":"config","voice":"F2","speed":1.1}` — 세션 중 변경(다음 text 부터). 이름은 `language`·`instructions`(hanulim 의 `lang`/`instruct_text` 아님). `voice_mode`·`sample_rate` 등 연결 고정값을 보내면 `CONFIG_LOCKED`.
   - `{"type":"ping"}` → `pong`. 300초 무입력이면 끊김.
   - `{"type":"end"}` — 세션 종료(`stream_end` 후 닫힘). **`utterance_end` 를 받은 뒤에만** 보낸다. text 직후 `end` 를 보내면 그동안 `cancel` 을 못 읽는다.
3. 서버 → 클라이언트:
   - **binary 프레임 = 헤더 없는 PCM**(`ready` 의 `sample_rate`, s16le, 모노) → 이어 붙인다.
   - JSON: `synthesis_start` · `synthesis_done`(`audio_sec`,`infer_sec`) · `utterance_end` · `config_updated`(`applied`) · `cancelled`(`frames_dropped`) · `pong` · `stream_end` · `error`.
   - `error` 는 `{"type":"error","code","message","fatal"}` — **`fatal: false` 는 연결 유지**(그 프레임만 버려짐: `VOICE_NOT_FOUND`·`VOICE_NOT_ALLOWED`·`CONFIG_LOCKED`·`INVALID_CONFIG`), **`fatal: true` 일 때만 세션 종료**. `IDLE_TIMEOUT` 은 fatal false 로 먼저 온 뒤 true 로 닫히니 판단은 `fatal` 로.

PoC 화면:

- 텍스트 입력 → [합성] → 수신한 PCM 을 위 `wave` 방식으로 WAV 로 감싸 `st.audio` 재생 + 다운로드. `ready.sample_rate`
로 헤더를 만든다.
- 표시: 첫 오디오 바이트까지 시간(TTFB), 총 오디오 길이(`stream_end.total_audio_sec`), 받은 이벤트 로그(`st.expander`).
- **끼어들기 테스트 버튼**: 긴 문장을 보내고 PCM 을 N초분 받은 뒤 `cancel` 을 보내 `cancelled` 가 오는지, 그 뒤 새 `text` 가 정상 합성되는지 확인. (이미 받아 둔 오디오를 멈추는 건 클라이언트 몫 — PoC 는 받은 만큼만 보여 주면 된다.)
- **세션 중 config 테스트**: 발화 사이에 `voice` 를 바꿔 `config_updated` 를 확인하고, 허용 범위 밖 목소리면 `VOICE_NOT_ALLOWED`(fatal false)가 오는지 본다.
- 수신은 스레드 + `queue.Queue` 로 하고 `st.*` 는 메인 스레드에서만 호출한다(STT WS 와 동일 규칙).

음성 변환(`/model/tts/voice-convert`)은 **스코프 아웃**.

## 7. 서비스 설정 반영 — 제품별 정리

| 제품 | 콘솔에서 지정하는 것 | 클라이언트가 생략하면 | 확인 방법 |
|---|---|---|---|
| LLM | 서비스의 LLM (provider+model) | 그 지정값 사용 | 응답 `provider`/`model` |
| STT | 서비스의 STT (provider+model) | 그 지정값 사용 | 응답 `provider` |
| TTS | 목소리 묶음(basket) + 허용 범위 | provider 생략 시 **플랫폼 기본 엔진**, voice 는 직접 골라야 함 | `/tts/basket`, `/tts/voices` |

테스트 절차(README 성격의 안내로 앱 하단 expander 에 넣어도 좋다):

1. 콘솔에서 서비스의 LLM/STT 를 바꾼다.
2. PoC 에서 **「서비스 설정 그대로」** 로 호출 → 응답의 provider/model 이 방금 바꾼 값인지 본다.
3. 허용 범위 밖 값을 「직접 지정」으로 보내 **403** 이 나는지 본다.
4. TTS 는 콘솔에서 묶음을 바꾸고 「연결 확인」을 다시 눌러 `/tts/basket` 이 바뀌는지 본다.

## 8. 구현 규칙

- `api_client.py` 에 공통 함수 `request(method, path, **kw)` 하나를 두고 Base URL 조합·인증 헤더·타임아웃·에러 정규화를 여기서만 한다. 탭 코드는 이 함수만 부른다.
- 타임아웃: 일반 30초, STT 동기/LLM 은 120초, 스트리밍은 connect 10초 + read 120초.
- 빈 값 필드는 **키 자체를 생략**한다(서버는 빈 문자열을 「생략」과 같게 다루지만, 0/false 같은 값은 그대로 값으로 받는다).
- 결과 화면 공통: 성공 시 `st.success` + 결과, 원본 응답 JSON 은 `st.expander("원본 응답")` (오디오 응답은 헤더만).
- 사이드바 입력이 바뀌면 `providers` 캐시를 무효화한다.
- 요청 로그에 API 키를 남기지 않는다. 화면에 헤더를 보여 줄 땐 키를 `hp_live_****` 로 마스킹.

## 9. 하지 않는 것 (스코프 아웃)

- 모델/목소리 설정 UI (콘솔 영역), 회원가입/로그인, DB, 로그 영구 저장
- 음성 변환(`/model/tts/voice-convert`), 목소리 등록(클론 생성)
- `transcribe/best*`(AI ENSEMBLE), `compare`, `rescore` — 실험용 역량
- LLM 도구 호출/구조화 출력 UI

## 10. 실행

```bash
pip install -r requirements.txt   # streamlit, requests, websocket-client
streamlit run app.py
```

## 부록: 엔드포인트 요약

| 용도 | 메서드 · 경로 (모두 `/api/v1` 아래) | 요청 | 응답 |
|---|---|---|---|
| 키 확인 | `GET /client/me` | — | JSON |
| 발견 | `GET /model/providers` | — | JSON |
| LLM | `POST /model/generate` | JSON (`messages`) | JSON 또는 SSE |
| STT 비동기 제출 | `POST /model/transcribe/async` | multipart (`audio`) | 202 JSON |
| STT 비동기 조회 | `GET /model/transcribe/async/{id}?provider=` | — | JSON |
| STT 실시간 | `GET /model/transcribe/stream?sample_rate=` (WS) | binary raw PCM16LE mono | JSON 프레임 `{text, final}` |
| TTS 실시간 | `GET /model/synthesize/ws?voice_mode=…` (WS) | JSON 제어 프레임 | binary PCM + JSON 이벤트 |
| TTS 합성 | `POST /model/synthesize` | JSON (`text`) | **오디오 바이트** |
| TTS 스트림 | `POST /model/synthesize/stream` | JSON (`text`) | **raw PCM** (`X-Sample-Rate`) |
| 목소리(전체) | `GET /model/tts/voices?model=` | — | JSON |
| 목소리(지정 묶음) | `GET /model/tts/basket` | — | JSON |
