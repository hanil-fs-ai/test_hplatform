"""ws_client.py 의 오프라인 순수 로직 테스트.

실제 소켓 없이: URL 빌더, WAV->PCM 변환, WsSession 의 프레임 분류/드레인
계약(fake recv_data 시퀀스로 스레드 없이 직접 호출), 파일 재생 청크 크기 계산.
"""
import io
import struct
import wave

import pytest
import websocket

import ws_client
from api_client import Conn
from ws_client import SttStreamSession, TtsWsSession, WsSession, build_stt_stream_url, build_tts_ws_url, wav_to_pcm16_mono


@pytest.fixture
def conn():
    return Conn(scheme="http", host="localhost", port=8080, api_key="hp_live_test123")


# ---- URL builders ----

def test_build_stt_stream_url_omits_unset_provider_model(conn):
    url = build_stt_stream_url(conn, sample_rate=16000)
    assert url == "ws://localhost:8080/api/v1/model/transcribe/stream?sample_rate=16000"


def test_build_stt_stream_url_includes_provider_and_model(conn):
    url = build_stt_stream_url(conn, sample_rate=16000, provider="whisper", model="base")
    assert "provider=whisper" in url
    assert "model=base" in url


def test_build_stt_stream_url_wss_for_https(conn):
    conn.scheme = "https"
    url = build_stt_stream_url(conn, sample_rate=16000)
    assert url.startswith("wss://")


def test_build_tts_ws_url_requires_voice_mode_present(conn):
    url = build_tts_ws_url(conn, voice_mode="preset", voice="F1", language="ko")
    assert "voice_mode=preset" in url
    assert "voice=F1" in url
    assert "language=ko" in url


def test_build_tts_ws_url_omits_unset_optional_fields(conn):
    url = build_tts_ws_url(conn, voice_mode="preset", voice="F1", language="ko")
    query = url.split("?", 1)[1]
    assert "speed" not in query
    assert "seed" not in query
    assert "model=" not in query


# ---- WAV -> PCM ----

def _make_wav_bytes(*, channels=1, sampwidth=2, framerate=16000, n_frames=1600):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        frame_count = n_frames
        samples = [1000] * (frame_count * channels)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    buf.seek(0)
    return buf


def test_wav_to_pcm16_mono_passthrough_for_mono_input():
    buf = _make_wav_bytes(channels=1, framerate=16000, n_frames=1600)
    pcm, rate = wav_to_pcm16_mono(buf)
    assert rate == 16000
    assert len(pcm) == 1600 * 2  # 16-bit mono samples


def test_wav_to_pcm16_mono_downmixes_stereo():
    buf = _make_wav_bytes(channels=2, framerate=16000, n_frames=800)
    pcm, rate = wav_to_pcm16_mono(buf)
    assert rate == 16000
    assert len(pcm) == 800 * 2  # downmixed to mono: one sample per frame


def test_wav_to_pcm16_mono_rejects_8bit():
    buf = _make_wav_bytes(sampwidth=1)
    with pytest.raises(ValueError):
        wav_to_pcm16_mono(buf)


# ---- WsSession frame classification (no real socket) ----

class _FakeWs:
    """recv_data() 를 미리 정해둔 시퀀스로 재생하는 가짜 소켓."""

    def __init__(self, frames):
        self._frames = list(frames)

    def recv_data(self):
        if not self._frames:
            raise websocket.WebSocketConnectionClosedException()
        return self._frames.pop(0)


def test_recv_loop_classifies_binary_and_json_and_closed():
    frames = [
        (websocket.ABNF.OPCODE_BINARY, b"\x01\x02"),
        (websocket.ABNF.OPCODE_TEXT, b'{"text": "hi", "final": false}'),
    ]
    session = WsSession(url="ws://example")
    session.ws = _FakeWs(frames)
    session._recv_loop()  # runs synchronously until _FakeWs raises closed

    items = session.drain()
    assert items[0] == ("binary", b"\x01\x02")
    assert items[1] == ("json", {"text": "hi", "final": False})
    assert items[2] == ("closed", None)


def test_recv_loop_classifies_non_json_text_as_text():
    frames = [(websocket.ABNF.OPCODE_TEXT, "not json")]
    session = WsSession(url="ws://example")
    session.ws = _FakeWs(frames)
    session._recv_loop()
    items = session.drain()
    assert items[0] == ("text", "not json")


def test_drain_is_nondestructive_across_calls_and_empties_queue():
    session = WsSession(url="ws://example")
    session.q.put(("json", {"a": 1}))
    first = session.drain()
    second = session.drain()
    assert first == [("json", {"a": 1})]
    assert second == []


def test_recv_loop_stops_on_stop_event_without_consuming_more():
    session = WsSession(url="ws://example")
    session.ws = _FakeWs([(websocket.ABNF.OPCODE_TEXT, '{"x":1}')])
    session._stop.set()
    session._recv_loop()  # loop condition false immediately
    assert session.drain() == []


# ---- STT mode B chunk size math ----

def test_start_file_playback_chunk_size_matches_100ms_at_16k(monkeypatch):
    sent_chunks = []
    session = SttStreamSession(url="ws://example")
    session.send_binary = lambda data: sent_chunks.append(data)
    monkeypatch.setattr(ws_client.time, "sleep", lambda s: None)

    pcm = b"\x00\x01" * 1600 * 3  # 3 chunks worth at 16kHz/100ms (3200 bytes/chunk)
    session.start_file_playback(pcm, sample_rate=16000, chunk_ms=100)

    # start_file_playback spawns a background thread; wait for it to finish.
    import time as _time
    for _ in range(50):
        if len(sent_chunks) >= 3 or session.q.qsize():
            break
        _time.sleep(0.02)

    assert all(len(c) == 3200 for c in sent_chunks)
    assert sent_chunks == [pcm[0:3200], pcm[3200:6400], pcm[6400:9600]]


# ---- TtsWsSession control frame helpers ----

def test_tts_ws_control_frames_shape():
    session = TtsWsSession(url="ws://example")
    sent = []
    session.send_json = lambda obj: sent.append(obj)

    session.send_text("안녕")
    session.flush()
    session.cancel()
    session.config(voice="F2", speed=1.1)
    session.ping()
    session.end()

    assert sent == [
        {"type": "text", "data": "안녕"},
        {"type": "flush", "final": True},
        {"type": "cancel"},
        {"type": "config", "voice": "F2", "speed": 1.1},
        {"type": "ping"},
        {"type": "end"},
    ]
