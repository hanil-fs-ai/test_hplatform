"""hplatform API 클라이언트 공통 함수.

Base URL 조합, 인증 헤더, 타임아웃, 에러 정규화를 여기서만 처리한다.
탭 코드는 이 모듈의 함수만 호출한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import requests


@dataclass
class Conn:
    scheme: str
    host: str
    port: int
    api_key: str

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}/api/v1"

    @property
    def ws_scheme(self) -> str:
        return "wss" if self.scheme == "https" else "ws"


class ApiError(Exception):
    """status == -1 은 HTTP 응답 자체를 못 받은 전송 계층 실패."""

    def __init__(self, status: int, message: str, raw: requests.Response | None = None):
        self.status = status
        self.message = message
        self.raw = raw
        super().__init__(f"{status} — {message}")


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    """None/"" 값만 제거. 0/False/[]/{} 는 실제 값으로 유지한다."""
    return {k: v for k, v in d.items() if v is not None and v != ""}


def _error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
        return resp.text
    except ValueError:
        return resp.text


def request(
    method: str,
    path: str,
    conn: Conn,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    files: dict[str, Any] | None = None,
    stream: bool = False,
    timeout: float | tuple[float, float] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> requests.Response:
    url = conn.base_url.rstrip("/") + "/" + path.lstrip("/")
    headers = {"X-API-Key": conn.api_key, **(extra_headers or {})}
    if timeout is None:
        timeout = (10, 120) if stream else 30
    try:
        resp = requests.request(
            method,
            url,
            params=_clean(params) if params else None,
            json=json_body,
            data=data,
            files=files,
            headers=headers,
            timeout=timeout,
            stream=stream,
        )
    except requests.exceptions.RequestException as e:
        raise ApiError(-1, str(e)) from e
    if resp.status_code >= 400:
        raise ApiError(resp.status_code, _error_message(resp), raw=resp)
    return resp


# ---- 발견 / 키 확인 ----

def client_me(conn: Conn) -> dict:
    return request("GET", "/client/me", conn).json()


def model_providers(conn: Conn) -> dict:
    return request("GET", "/model/providers", conn).json()


# ---- LLM ----

def llm_generate(
    conn: Conn,
    *,
    messages: list[dict[str, str]],
    system: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    stop: list[str] | None = None,
) -> dict:
    body = _clean(
        {
            "messages": messages,
            "system": system,
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stop": stop,
            "stream": False,
        }
    )
    return request("POST", "/model/generate", conn, json_body=body, timeout=120).json()


def llm_generate_stream(
    conn: Conn,
    *,
    messages: list[dict[str, str]],
    system: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    top_p: float | None = None,
    stop: list[str] | None = None,
) -> Iterator[dict]:
    """SSE 프레임을 하나씩 파싱해 yield 하는 제너레이터.

    스트림 시작 전 실패(400/403 등)는 request() 가 ApiError 로 던진다.
    `data: ` 접두사를 뗀 뒤 json.loads 하며, `[DONE]` 센티넬은 없고
    `done: true` 프레임으로 종료를 판단한다.
    """
    body = _clean(
        {
            "messages": messages,
            "system": system,
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
            "stop": stop,
            "stream": True,
        }
    )
    resp = request("POST", "/model/generate", conn, json_body=body, stream=True, timeout=(10, 120))
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        try:
            frame = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        yield frame
        if frame.get("done"):
            break


# ---- STT ----

def stt_transcribe(
    conn: Conn,
    *,
    audio_bytes: bytes,
    filename: str,
    mime: str,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
) -> dict:
    files = {"audio": (filename, audio_bytes, mime)}
    data = _clean({"provider": provider, "model": model, "language": language})
    return request(
        "POST", "/model/transcribe", conn, files=files, data=data or None, timeout=120
    ).json()


def stt_transcribe_async_submit(
    conn: Conn,
    *,
    audio_bytes: bytes,
    filename: str,
    mime: str,
    provider: str | None = None,
    model: str | None = None,
    language: str | None = None,
) -> dict:
    files = {"audio": (filename, audio_bytes, mime)}
    data = _clean({"provider": provider, "model": model, "language": language})
    return request(
        "POST", "/model/transcribe/async", conn, files=files, data=data or None, timeout=30
    ).json()


def stt_transcribe_async_poll(conn: Conn, *, job_id: str, provider: str) -> dict:
    return request(
        "GET", f"/model/transcribe/async/{job_id}", conn, params={"provider": provider}, timeout=30
    ).json()


# ---- TTS ----

def tts_voices(conn: Conn, *, model: str, provider: str | None = None) -> dict:
    return request(
        "GET", "/model/tts/voices", conn, params={"model": model, "provider": provider}, timeout=30
    ).json()


def tts_basket(conn: Conn) -> dict:
    return request("GET", "/model/tts/basket", conn, timeout=30).json()


def tts_synthesize(
    conn: Conn,
    *,
    text: str,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    voice_mode: str | None = None,
    language: str | None = None,
    speed: float | None = None,
    instructions: str | None = None,
    seed: int | None = None,
    output_check: bool | None = None,
) -> tuple[bytes, dict]:
    body = _clean(
        {
            "text": text,
            "provider": provider,
            "model": model,
            "voice": voice,
            "voice_mode": voice_mode,
            "language": language,
            "speed": speed,
            "instructions": instructions,
            "seed": seed,
            "output_check": output_check,
        }
    )
    resp = request("POST", "/model/synthesize", conn, json_body=body, timeout=120)
    return resp.content, dict(resp.headers)


def tts_synthesize_stream(
    conn: Conn,
    *,
    text: str,
    provider: str | None = None,
    model: str | None = None,
    voice: str | None = None,
    voice_mode: str | None = None,
    language: str | None = None,
    speed: float | None = None,
    instructions: str | None = None,
    seed: int | None = None,
    stream_mode: str | None = None,
) -> tuple[Iterator[bytes], dict]:
    body = _clean(
        {
            "text": text,
            "provider": provider,
            "model": model,
            "voice": voice,
            "voice_mode": voice_mode,
            "language": language,
            "speed": speed,
            "instructions": instructions,
            "seed": seed,
            "stream_mode": stream_mode,
        }
    )
    resp = request(
        "POST", "/model/synthesize/stream", conn, json_body=body, stream=True, timeout=(10, 120)
    )
    headers = dict(resp.headers)

    def _bytes() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
        except requests.exceptions.ChunkedEncodingError as e:
            raise ApiError(-1, f"스트림이 중간에 끊김: {e}") from e

    return _bytes(), headers
