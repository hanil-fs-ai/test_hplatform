"""STT/TTS WebSocket 수신 스레드 + queue 헬퍼.

스레드는 큐에만 넣고, st.* 호출은 메인(Streamlit 스크립트) 스레드가
drain() 으로 큐를 비울 때만 한다.
"""
from __future__ import annotations

import audioop
import io
import json
import queue
import threading
import time
import wave
from typing import Any, Iterator
from urllib.parse import urlencode

import websocket

from api_client import Conn, _clean


def wav_to_pcm16_mono(file_obj: io.IOBase) -> tuple[bytes, int]:
    """WAV 파일에서 16-bit PCM, 모노 프레임과 표본율을 꺼낸다.

    스테레오면 다운믹스한다. 16-bit 가 아니면 ValueError.
    """
    with wave.open(file_obj, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError("16-bit PCM WAV 파일만 지원합니다")
        channels = w.getnchannels()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    if channels > 1:
        frames = audioop.tomono(frames, 2, 0.5, 0.5)
    return frames, rate


class WsSession:
    """websocket-client 위의 공통 수신 스레드 래퍼.

    binary/text 프레임을 opcode 로 구분해 (kind, payload) 튜플로
    큐에 적재한다. kind 는 "binary" | "json" | "text" | "closed" | "error".
    """

    def __init__(
        self,
        url: str,
        header: list[str] | None = None,
        subprotocols: list[str] | None = None,
    ):
        self.url = url
        self.header = header or []
        self.subprotocols = subprotocols
        self.q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.ws: websocket.WebSocket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def connect(self, timeout: float = 10) -> None:
        self.ws = websocket.create_connection(
            self.url, header=self.header, subprotocols=self.subprotocols, timeout=timeout
        )
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()

    def _recv_loop(self) -> None:
        while not self._stop.is_set():
            try:
                opcode, data = self.ws.recv_data()
            except websocket.WebSocketConnectionClosedException:
                self.q.put(("closed", None))
                return
            except Exception as e:  # noqa: BLE001 - surface any recv failure to the queue
                self.q.put(("error", str(e)))
                return
            if opcode == websocket.ABNF.OPCODE_BINARY:
                self.q.put(("binary", data))
            elif opcode == websocket.ABNF.OPCODE_TEXT:
                text = data.decode("utf-8") if isinstance(data, bytes) else data
                try:
                    self.q.put(("json", json.loads(text)))
                except json.JSONDecodeError:
                    self.q.put(("text", text))
            elif opcode == websocket.ABNF.OPCODE_CLOSE:
                self.q.put(("closed", None))
                return

    def send_json(self, obj: dict) -> None:
        self.ws.send(json.dumps(obj))

    def send_binary(self, data: bytes) -> None:
        self.ws.send_binary(data)

    def close(self) -> None:
        self._stop.set()
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:  # noqa: BLE001 - best-effort close
            pass

    def drain(self) -> list[tuple[str, Any]]:
        """큐에 쌓인 항목을 모두 non-blocking 으로 꺼낸다. 메인 스레드에서만 호출."""
        items: list[tuple[str, Any]] = []
        while True:
            try:
                items.append(self.q.get_nowait())
            except queue.Empty:
                break
        return items


def build_stt_stream_url(
    conn: Conn, sample_rate: int, provider: str | None = None, model: str | None = None
) -> str:
    q = urlencode(_clean({"sample_rate": sample_rate, "provider": provider, "model": model}))
    return f"{conn.ws_scheme}://{conn.host}:{conn.port}/api/v1/model/transcribe/stream?{q}"


class SttStreamSession(WsSession):
    """모드 B: 업로드 파일을 실시간 속도로 흘려보내는 STT 스트림 세션."""

    def start_file_playback(self, pcm: bytes, sample_rate: int, chunk_ms: int = 100) -> None:
        chunk_bytes = int(sample_rate * chunk_ms / 1000) * 2  # 16-bit mono

        def _sender() -> None:
            for offset in range(0, len(pcm), chunk_bytes):
                if self._stop.is_set():
                    return
                self.send_binary(pcm[offset : offset + chunk_bytes])
                time.sleep(chunk_ms / 1000)
            self.q.put(("send_done", None))

        threading.Thread(target=_sender, daemon=True).start()


def build_tts_ws_url(
    conn: Conn,
    *,
    voice_mode: str,
    voice: str | None = None,
    language: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    speed: float | None = None,
    seed: int | None = None,
    stream_mode: str | None = None,
    sample_rate: int | None = None,
    pcm_format: str | None = None,
    instructions: str | None = None,
) -> str:
    q = urlencode(
        _clean(
            {
                "voice_mode": voice_mode,
                "voice": voice,
                "language": language,
                "model": model,
                "provider": provider,
                "speed": speed,
                "seed": seed,
                "stream_mode": stream_mode,
                "sample_rate": sample_rate,
                "pcm_format": pcm_format,
                "instructions": instructions,
            }
        )
    )
    return f"{conn.ws_scheme}://{conn.host}:{conn.port}/api/v1/model/synthesize/ws?{q}"


class TtsWsSession(WsSession):
    """실시간 합성(TTS WS) 세션. 제어 프레임 송신 헬퍼만 얹는다."""

    def send_text(self, text: str) -> None:
        self.send_json({"type": "text", "data": text})

    def flush(self) -> None:
        self.send_json({"type": "flush", "final": True})

    def cancel(self) -> None:
        self.send_json({"type": "cancel"})

    def config(self, **kwargs: Any) -> None:
        self.send_json({"type": "config", **kwargs})

    def ping(self) -> None:
        self.send_json({"type": "ping"})

    def end(self) -> None:
        self.send_json({"type": "end"})
