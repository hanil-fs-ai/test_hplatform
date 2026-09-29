"""api_client.py 의 오프라인(서버 불필요) 순수 로직 테스트.

requests.request 를 mock 하여 URL 조합, 헤더, 타임아웃, 에러 정규화,
SSE 파싱, TTS 스트림 mid-stream 에러 래핑을 검증한다.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

import api_client
from api_client import ApiError, Conn, _clean


@pytest.fixture
def conn():
    return Conn(scheme="http", host="localhost", port=8080, api_key="hp_live_test123")


# ---- _clean ----

def test_clean_drops_none_and_empty_string():
    assert _clean({"a": None, "b": "", "c": "x"}) == {"c": "x"}


def test_clean_keeps_falsy_non_empty_values():
    assert _clean({"a": 0, "b": False, "c": [], "d": {}}) == {"a": 0, "b": False, "c": [], "d": {}}


# ---- request(): URL / headers / timeout ----

def _make_response(status_code=200, json_body=None, text="", headers=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


@patch("api_client.requests.request")
def test_request_builds_base_url_and_auth_header(mock_req, conn):
    mock_req.return_value = _make_response(200, {"ok": True})
    api_client.request("GET", "/client/me", conn)
    args, kwargs = mock_req.call_args
    assert args[0] == "GET"
    assert args[1] == "http://localhost:8080/api/v1/client/me"
    assert kwargs["headers"]["X-API-Key"] == "hp_live_test123"
    assert kwargs["timeout"] == 30


@patch("api_client.requests.request")
def test_request_strips_leading_slash_duplication(mock_req, conn):
    mock_req.return_value = _make_response(200, {})
    api_client.request("GET", "model/providers", conn)
    url = mock_req.call_args.args[1]
    assert url == "http://localhost:8080/api/v1/model/providers"


@patch("api_client.requests.request")
def test_request_stream_default_timeout(mock_req, conn):
    mock_req.return_value = _make_response(200, {})
    api_client.request("POST", "/model/generate", conn, stream=True)
    assert mock_req.call_args.kwargs["timeout"] == (10, 120)


@patch("api_client.requests.request")
def test_request_explicit_timeout_wins(mock_req, conn):
    mock_req.return_value = _make_response(200, {})
    api_client.request("POST", "/model/transcribe", conn, timeout=120)
    assert mock_req.call_args.kwargs["timeout"] == 120


@patch("api_client.requests.request")
def test_request_params_are_cleaned(mock_req, conn):
    mock_req.return_value = _make_response(200, {})
    api_client.request("GET", "/model/tts/voices", conn, params={"model": "m", "provider": ""})
    assert mock_req.call_args.kwargs["params"] == {"model": "m"}


# ---- error normalization ----

@patch("api_client.requests.request")
def test_error_response_with_json_message(mock_req, conn):
    mock_req.return_value = _make_response(403, {"message": "provider not allowed"})
    with pytest.raises(ApiError) as exc_info:
        api_client.request("POST", "/model/generate", conn)
    assert exc_info.value.status == 403
    assert exc_info.value.message == "provider not allowed"


@patch("api_client.requests.request")
def test_error_response_falls_back_to_text_when_not_json(mock_req, conn):
    resp = _make_response(502, text="<html>Bad Gateway</html>")
    mock_req.return_value = resp
    with pytest.raises(ApiError) as exc_info:
        api_client.request("POST", "/model/generate", conn)
    assert exc_info.value.status == 502
    assert exc_info.value.message == "<html>Bad Gateway</html>"


@patch("api_client.requests.request")
def test_transport_failure_raises_apierror_status_minus_one(mock_req, conn):
    mock_req.side_effect = requests.exceptions.ConnectionError("refused")
    with pytest.raises(ApiError) as exc_info:
        api_client.request("GET", "/client/me", conn)
    assert exc_info.value.status == -1


# ---- LLM: messages omit-empty rule ----

@patch("api_client.requests.request")
def test_llm_generate_omits_unset_optional_fields(mock_req, conn):
    mock_req.return_value = _make_response(200, {"provider": "deepseek", "model": "deepseek-chat", "content": "hi"})
    api_client.llm_generate(conn, messages=[{"role": "user", "content": "hi"}])
    body = mock_req.call_args.kwargs["json"]
    assert "provider" not in body
    assert "model" not in body
    assert "temperature" not in body
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["stream"] is False


@patch("api_client.requests.request")
def test_llm_generate_keeps_zero_temperature(mock_req, conn):
    mock_req.return_value = _make_response(200, {"provider": "p", "model": "m", "content": "x"})
    api_client.llm_generate(conn, messages=[{"role": "user", "content": "hi"}], temperature=0)
    body = mock_req.call_args.kwargs["json"]
    assert body["temperature"] == 0


# ---- SSE stream parsing ----

def _sse_lines(*frames):
    """실제 requests.Response.iter_lines() 처럼 raw bytes 줄을 만든다
    (UTF-8 인코딩; decode_unicode=True 를 안 쓰는 게 이번 수정의 핵심)."""
    lines = []
    for f in frames:
        lines.append(f"data: {json.dumps(f, ensure_ascii=False)}".encode("utf-8"))
        lines.append(b"")
    return lines


@patch("api_client.requests.request")
def test_llm_generate_stream_parses_frames_until_done(mock_req, conn):
    frames = [
        {"content_delta": "안", "model": "deepseek-chat"},
        {"content_delta": "녕"},
        {"finish_reason": "stop", "usage": {"total_tokens": 10}, "done": True},
    ]
    resp = _make_response(200)
    resp.iter_lines.return_value = iter(_sse_lines(*frames))
    mock_req.return_value = resp

    out = list(api_client.llm_generate_stream(conn, messages=[{"role": "user", "content": "hi"}]))
    assert out == frames


@patch("api_client.requests.request")
def test_llm_generate_stream_stops_at_first_done_frame(mock_req, conn):
    frames = [{"done": True}, {"content_delta": "should not appear"}]
    resp = _make_response(200)
    resp.iter_lines.return_value = iter(_sse_lines(*frames))
    mock_req.return_value = resp

    out = list(api_client.llm_generate_stream(conn, messages=[{"role": "user", "content": "hi"}]))
    assert out == [{"done": True}]


@patch("api_client.requests.request")
def test_llm_generate_stream_skips_malformed_lines(mock_req, conn):
    resp = _make_response(200)
    resp.iter_lines.return_value = iter(
        [b"", b"not-a-data-line", b"data: {bad json", b'data: {"done": true}']
    )
    mock_req.return_value = resp

    out = list(api_client.llm_generate_stream(conn, messages=[{"role": "user", "content": "hi"}]))
    assert out == [{"done": True}]


@patch("api_client.requests.request")
def test_llm_generate_stream_decodes_utf8_even_when_resp_encoding_is_wrong(mock_req, conn):
    """회귀 테스트: 서버가 Content-Type 에 charset 을 안 붙이면 requests 는
    resp.encoding 을 ISO-8859-1 로 잘못 추정한다. decode_unicode=True 를 쓰면
    이 값으로 디코딩되어 한글이 깨지므로, resp.encoding 과 무관하게 항상
    UTF-8 로 직접 디코딩해야 한다."""
    frame = {"content_delta": "안녕하세요 한글 테스트"}
    resp = _make_response(200)
    resp.encoding = "ISO-8859-1"  # requests 의 흔한 오추정 시나리오
    resp.iter_lines.return_value = iter(_sse_lines(frame, {"done": True}))
    mock_req.return_value = resp

    out = list(api_client.llm_generate_stream(conn, messages=[{"role": "user", "content": "hi"}]))
    assert out[0]["content_delta"] == "안녕하세요 한글 테스트"


# ---- STT multipart shape ----

@patch("api_client.requests.request")
def test_stt_async_submit_sends_audio_part_named_audio(mock_req, conn):
    mock_req.return_value = _make_response(202, {"id": "j", "provider": "whisper"})
    api_client.stt_transcribe_async_submit(conn, audio_bytes=b"RIFF....", filename="a.wav", mime="audio/wav")
    files = mock_req.call_args.kwargs["files"]
    assert files["audio"] == ("a.wav", b"RIFF....", "audio/wav")


@patch("api_client.requests.request")
def test_stt_async_submit_omits_empty_language(mock_req, conn):
    mock_req.return_value = _make_response(202, {"id": "j", "provider": "whisper"})
    api_client.stt_transcribe_async_submit(conn, audio_bytes=b"x", filename="a.wav", mime="audio/wav", language="")
    data = mock_req.call_args.kwargs["data"]
    assert data is None or "language" not in data


# ---- STT async: provider carry-over ----

@patch("api_client.requests.request")
def test_stt_async_submit_returns_id_and_provider(mock_req, conn):
    mock_req.return_value = _make_response(202, {"id": "job-1", "provider": "whisper"})
    result = api_client.stt_transcribe_async_submit(
        conn, audio_bytes=b"x", filename="a.wav", mime="audio/wav"
    )
    assert result == {"id": "job-1", "provider": "whisper"}


@patch("api_client.requests.request")
def test_stt_async_poll_uses_given_provider_as_query_param(mock_req, conn):
    mock_req.return_value = _make_response(200, {"status": "pending"})
    api_client.stt_transcribe_async_poll(conn, job_id="job-1", provider="whisper")
    assert mock_req.call_args.kwargs["params"] == {"provider": "whisper"}
    assert mock_req.call_args.args[1].endswith("/model/transcribe/async/job-1")


# ---- TTS: no-JSON-parse on success, headers preserved ----

@patch("api_client.requests.request")
def test_tts_synthesize_returns_raw_bytes_and_headers(mock_req, conn):
    resp = _make_response(200, headers={"Content-Type": "audio/wav"})
    resp.content = b"\x00\x01\x02"
    mock_req.return_value = resp
    audio, headers = api_client.tts_synthesize(conn, text="hi")
    assert audio == b"\x00\x01\x02"
    assert headers["Content-Type"] == "audio/wav"
    resp.json.assert_not_called()


@patch("api_client.requests.request")
def test_tts_synthesize_omits_unset_speed(mock_req, conn):
    resp = _make_response(200, headers={"Content-Type": "audio/wav"})
    resp.content = b""
    mock_req.return_value = resp
    api_client.tts_synthesize(conn, text="hi", speed=None)
    body = mock_req.call_args.kwargs["json"]
    assert "speed" not in body


@patch("api_client.requests.request")
def test_tts_synthesize_stream_wraps_mid_stream_error(mock_req, conn):
    resp = _make_response(200, headers={"X-Sample-Rate": "44100"})

    def _raising_iter_content(chunk_size):
        yield b"partial-audio"
        raise requests.exceptions.ChunkedEncodingError("connection broken")

    resp.iter_content.side_effect = _raising_iter_content
    mock_req.return_value = resp

    chunks, headers = api_client.tts_synthesize_stream(conn, text="hi")
    assert headers["X-Sample-Rate"] == "44100"
    collected = []
    with pytest.raises(ApiError) as exc_info:
        for chunk in chunks:
            collected.append(chunk)
    assert collected == [b"partial-audio"]
    assert exc_info.value.status == -1
