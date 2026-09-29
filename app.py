"""hplatform 클라이언트 PoC — Streamlit 앱.

외부 챗봇/업무 시스템이 hplatform 을 호출하는 것과 동일한 방식으로
LLM/STT/TTS API 를 호출해, 콘솔에서 서비스에 지정한 설정이 실제
호출에 반영되는지 확인하기 위한 테스트 도구.
"""
from __future__ import annotations

import io
import json
import pathlib
import time
import wave

import streamlit as st

import api_client
import ws_client
from api_client import ApiError, Conn

st.set_page_config(page_title="hplatform PoC", layout="wide")

# st.audio_input 의 시작/멈춤(마이크) 버튼이 너무 작아서 위젯 전체를 키운다.
st.markdown(
    """
    <style>
    [data-testid="stAudioInput"] { zoom: 1.6; }
    [data-testid="stAudioInputActionButton"] { min-width: 2.5rem; min-height: 2.5rem; }
    </style>
    """,
    unsafe_allow_html=True,
)

APP_DIR = pathlib.Path(__file__).parent


# ---------------------------------------------------------------------------
# 공통 헬퍼
# ---------------------------------------------------------------------------

def _build_conn() -> Conn:
    return Conn(
        scheme=st.session_state.get("sb_scheme", "http"),
        host=st.session_state.get("sb_host", "localhost"),
        port=int(st.session_state.get("sb_port", 8080) or 8080),
        api_key=st.session_state.get("sb_api_key", ""),
    )


def _sig(conn: Conn) -> tuple:
    return (conn.scheme, conn.host, conn.port, conn.api_key)


def show_error(e: ApiError) -> None:
    st.error(f"{e.status} — {e.message}")


# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------

def render_sidebar() -> Conn:
    with st.sidebar:
        st.header("서버 / 인증")
        st.selectbox("스킴", ["http", "https"], key="sb_scheme")
        st.text_input("Host", value=st.session_state.get("sb_host", "localhost"), key="sb_host")
        st.number_input("Port", value=st.session_state.get("sb_port", 8080), step=1, key="sb_port")
        st.text_input("API 키", type="password", key="sb_api_key")

        if st.button("연결 확인", use_container_width=True):
            conn = _build_conn()
            try:
                me = api_client.client_me(conn)
                providers = api_client.model_providers(conn)
                st.session_state["client_me"] = me
                st.session_state["providers"] = providers
                st.session_state["providers_sig"] = _sig(conn)
                st.session_state["tts_basket_cache"] = None
                st.session_state["tts_voices_cache"] = {}
                st.success("연결 성공")
            except ApiError as e:
                show_error(e)

        conn = _build_conn()
        if st.session_state.get("providers") is not None and st.session_state.get("providers_sig") != _sig(conn):
            st.warning("설정이 바뀌었습니다. 연결 확인을 다시 누르세요.")

        me = st.session_state.get("client_me")
        if me:
            st.caption(f"{me.get('name', '?')} · tenant={me.get('tenant_id', '?')} · {me.get('clearance', '?')}")
            scopes = me.get("scopes")
            if scopes:
                st.caption("scopes: " + ", ".join(scopes))

        key = st.session_state.get("sb_api_key", "")
        if key:
            masked = key[:8] + "****" if len(key) > 8 else "****"
            st.caption(f"key: {masked}")

        return conn


# ---------------------------------------------------------------------------
# LLM 탭
# ---------------------------------------------------------------------------

def _prompt_components_form() -> dict | None:
    """prompt_components 입력 폼. 빈 항목은 키를 생략한다."""
    sp = st.text_area("system_prompt", key="pc_system_prompt")
    ctx = st.text_area("context (선택 — 참고 자료)", key="pc_context")
    ctx_label = st.text_input("context_label (선택, 생략 시 '참고 정보')", key="pc_context_label")
    rules = st.text_area("output_rules (선택)", key="pc_output_rules")
    st.caption("fewshots (선택 — 최대 20쌍, 합쳐서 8000자)")
    n = int(st.number_input("예시 쌍 개수", min_value=0, max_value=20, value=0, step=1, key="pc_n_fewshots"))
    fewshots = []
    for i in range(n):
        c1, c2 = st.columns(2)
        u = c1.text_input(f"user {i + 1}", key=f"pc_fs_user_{i}")
        a = c2.text_input(f"assistant {i + 1}", key=f"pc_fs_asst_{i}")
        if u or a:
            fewshots.append({"user": u, "assistant": a})
    comp = {
        "system_prompt": sp,
        "context": ctx,
        "context_label": ctx_label,
        "output_rules": rules,
        "fewshots": fewshots,
    }
    comp = {k: v for k, v in comp.items() if v}
    return comp or None


def render_llm_tab(conn: Conn, providers: dict | None) -> None:
    if not providers or not providers.get("llm"):
        st.warning("이 설치에 LLM 이 연결되어 있지 않습니다.")
        return

    mode = st.radio("모델 선택", ["서비스 설정 그대로", "직접 지정"], key="llm_mode", horizontal=True)
    provider = model = None
    if mode == "직접 지정":
        provider = st.selectbox("Provider", providers["llm"], key="llm_provider")
        models = providers.get("llm_models", {}).get(provider) or []
        model = st.selectbox("Model", models, key="llm_model") if models else None

    with st.expander("고급 옵션"):
        sys_mode = st.radio(
            "system 프롬프트 방식",
            ["단순 (system)", "구성 요소 (prompt_components)"],
            key="llm_sys_mode",
            horizontal=True,
        )
        system = None
        prompt_components = None
        if sys_mode == "단순 (system)":
            system = st.text_area("system 지시 (선택)", key="llm_system")
        else:
            prompt_components = _prompt_components_form()
        use_temp = st.checkbox("temperature 사용", key="llm_use_temp")
        # Anthropic(claude)은 0..1 만 받는다. 서비스 설정 그대로면 provider 를 모르므로 1.0 으로 제한.
        temp_max = 2.0 if provider and provider != "claude" else 1.0
        temperature = (
            st.slider("temperature", 0.0, temp_max, 0.7, key=f"llm_temp_{temp_max}") if use_temp else None
        )
        if use_temp and temp_max == 1.0:
            st.caption("서비스 설정 그대로 / claude 는 temperature 0~1 만 허용")
        use_max_tokens = st.checkbox("max_tokens 사용", key="llm_use_max_tokens")
        max_tokens = (
            int(st.number_input("max_tokens", min_value=1, value=512, step=1, key="llm_max_tokens"))
            if use_max_tokens
            else None
        )
        use_top_p = st.checkbox("top_p 사용", key="llm_use_top_p")
        top_p = st.slider("top_p", 0.0, 1.0, 1.0, key="llm_top_p") if use_top_p else None
        use_stop = st.checkbox("stop 사용", key="llm_use_stop")
        stop = None
        if use_stop:
            stop_raw = st.text_input("stop (쉼표로 구분)", key="llm_stop_raw")
            stop = [s.strip() for s in stop_raw.split(",") if s.strip()] or None

    streaming = st.checkbox("스트리밍", value=False, key="llm_streaming")

    if st.button("대화 초기화", key="llm_reset"):
        st.session_state["messages"] = []
        st.session_state["llm_last_meta"] = None

    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("llm_last_meta", None)

    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    meta = st.session_state.get("llm_last_meta")
    if meta:
        c1, c2 = st.columns(2)
        c1.metric("실제 사용된 Provider", meta.get("provider") or "(스트림 응답엔 없음)")
        c2.metric("실제 사용된 Model", meta.get("model") or "?")

    user_input = st.chat_input("메시지를 입력하세요")
    if user_input:
        st.session_state["messages"].append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.write(user_input)

        with st.chat_message("assistant"):
            try:
                if streaming:
                    frame_meta: dict = {}

                    def _gen():
                        for frame in api_client.llm_generate_stream(
                            conn,
                            messages=st.session_state["messages"],
                            system=system or None,
                            prompt_components=prompt_components,
                            provider=provider,
                            model=model,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            top_p=top_p,
                            stop=stop,
                        ):
                            if "model" in frame and "model" not in frame_meta:
                                frame_meta["model"] = frame["model"]
                            if frame.get("temperature_unsupported"):
                                frame_meta["temperature_unsupported"] = True
                            if "finish_reason" in frame:
                                frame_meta["finish_reason"] = frame["finish_reason"]
                            if "usage" in frame:
                                frame_meta["usage"] = frame["usage"]
                            if frame.get("error"):
                                frame_meta["error"] = frame["error"]
                            if frame.get("content_delta"):
                                yield frame["content_delta"]

                    content = st.write_stream(_gen())
                    st.session_state["messages"].append({"role": "assistant", "content": content})
                    st.session_state["llm_last_meta"] = frame_meta
                    if frame_meta.get("temperature_unsupported"):
                        st.caption("⚠️ 이 모델이 temperature 를 거부해 빼고 재시도했습니다.")
                    if frame_meta.get("error"):
                        st.error(f"스트림 중 에러: {frame_meta['error']}")
                    with st.expander("원본 응답 (프레임 메타)"):
                        st.json(frame_meta)
                else:
                    resp = api_client.llm_generate(
                        conn,
                        messages=st.session_state["messages"],
                        system=system or None,
                        prompt_components=prompt_components,
                        provider=provider,
                        model=model,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        top_p=top_p,
                        stop=stop,
                    )
                    st.write(resp.get("content", ""))
                    st.session_state["messages"].append({"role": "assistant", "content": resp.get("content", "")})
                    st.session_state["llm_last_meta"] = resp
                    if resp.get("usage"):
                        st.caption(f"usage: {resp['usage']}")
                    with st.expander("원본 응답"):
                        st.json(resp)
            except ApiError as e:
                show_error(e)


# ---------------------------------------------------------------------------
# STT 탭
# ---------------------------------------------------------------------------

def render_stt_tab(conn: Conn, providers: dict | None) -> None:
    if not providers or not providers.get("stt"):
        st.warning("이 설치에 STT 가 연결되어 있지 않습니다.")
        return

    source = st.radio("입력 방식", ["마이크 녹음", "파일 업로드"], key="stt_source", horizontal=True)
    audio_bytes = None
    filename = "audio.wav"
    mime = "audio/wav"
    if source == "마이크 녹음":
        rec = st.audio_input("녹음", key="stt_audio_input")
        if rec is not None:
            audio_bytes = rec.getvalue()
            mime = rec.type or "audio/wav"
            filename = "recording.wav"
    else:
        up = st.file_uploader("오디오 파일", type=["wav", "mp3", "m4a", "flac", "ogg", "webm"], key="stt_file")
        if up is not None:
            audio_bytes = up.getvalue()
            mime = up.type or "application/octet-stream"
            filename = up.name

    language = st.text_input("language (BCP-47, 선택)", value="ko", key="stt_language")

    mode = st.radio("모델 선택", ["서비스 설정 그대로", "직접 지정"], key="stt_mode", horizontal=True)
    provider = model = None
    if mode == "직접 지정":
        provider = st.selectbox("Provider", providers["stt"], key="stt_provider")
        model = st.text_input("Model (자유 입력, 선택 — 목록 없음)", key="stt_model") or None

    st.caption("전사는 비동기(제출 → 폴링)로만 동작합니다.")

    if st.button("제출", disabled=audio_bytes is None):
        try:
            submit = api_client.stt_transcribe_async_submit(
                conn,
                audio_bytes=audio_bytes,
                filename=filename,
                mime=mime,
                provider=provider,
                model=model,
                language=language or None,
            )
            st.session_state["stt_async_job"] = submit
        except ApiError as e:
            show_error(e)

    job = st.session_state.get("stt_async_job")
    if job:
        st.caption(f"job id: {job.get('id')} (provider={job.get('provider')})")
        if st.button("상태 폴링 시작"):
            placeholder = st.empty()
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    status = api_client.stt_transcribe_async_poll(
                        conn, job_id=job["id"], provider=job["provider"]
                    )
                except ApiError as e:
                    placeholder.error(f"{e.status} — {e.message}")
                    break
                st_status = status.get("status")
                if st_status == "done":
                    placeholder.success(status.get("text", ""))
                    break
                if st_status == "failed":
                    placeholder.error(status.get("message", "실패"))
                    break
                placeholder.info("처리 중...")
                time.sleep(2.5)
            else:
                placeholder.warning("최대 대기 시간(5분) 초과")

    st.divider()
    st.subheader("실시간 (WebSocket)")
    rt_mode = st.radio("모드", ["A. 브라우저 마이크", "B. 파일 재생"], key="stt_rt_mode", horizontal=True)
    rt_provider = provider if mode == "직접 지정" else None
    rt_model = model if mode == "직접 지정" else None

    if rt_mode.startswith("A"):
        st.warning(
            "getUserMedia 는 보안 컨텍스트(http://localhost 또는 https://)에서만 동작합니다. "
            "Streamlit 이 https 로 서비스되면 허브도 wss:// 여야 합니다(mixed content). "
            "components.html 은 iframe 이라 마이크 권한 프롬프트가 뜰 수 있습니다."
        )
        ws_url = ws_client.build_stt_stream_url(conn, sample_rate=16000, provider=rt_provider, model=rt_model)
        template = (APP_DIR / "mic_stt.html").read_text(encoding="utf-8")
        html = template.replace("{{WS_URL}}", json.dumps(ws_url)).replace(
            "{{API_KEY}}", json.dumps(conn.api_key)
        )
        st.components.v1.html(html, height=280)
    else:
        rt_file = st.file_uploader("재생할 WAV 파일", type=["wav"], key="stt_rt_file")
        if st.button("스트림 시작", disabled=rt_file is None):
            try:
                pcm, rate = ws_client.wav_to_pcm16_mono(io.BytesIO(rt_file.getvalue()))
            except ValueError as e:
                st.error(str(e))
            else:
                url = ws_client.build_stt_stream_url(conn, sample_rate=rate, provider=rt_provider, model=rt_model)
                session = ws_client.SttStreamSession(url, header=[f"X-API-Key: {conn.api_key}"])
                try:
                    session.connect()
                except Exception as e:  # noqa: BLE001
                    st.error(f"WS 연결 실패: {e}")
                else:
                    session.start_file_playback(pcm, rate)
                    st.caption("⚠️ 소켓을 닫아도 마지막 확정(final) 결과가 나온다는 보장이 없습니다.")
                    partial_ph = st.empty()
                    final_ph = st.empty()
                    final_text = ""
                    sent_done = False
                    last_frame_time = time.time()
                    while True:
                        stop_now = False
                        for kind, payload in session.drain():
                            if kind == "send_done":
                                sent_done = True
                            elif kind == "json":
                                last_frame_time = time.time()
                                if payload.get("status") == "failed":
                                    st.error(payload.get("message", "실패"))
                                    stop_now = True
                                elif payload.get("final", False):
                                    final_text += (" " if final_text else "") + payload.get("text", "")
                                    final_ph.markdown(final_text)
                                    partial_ph.empty()
                                else:
                                    partial_ph.markdown(f":gray[{payload.get('text', '')}]")
                            elif kind in ("closed", "error"):
                                stop_now = True
                        if stop_now:
                            break
                        if sent_done and time.time() - last_frame_time > 3.0:
                            break
                        time.sleep(0.05)
                    session.close()


# ---------------------------------------------------------------------------
# TTS 탭
# ---------------------------------------------------------------------------

def render_tts_tab(conn: Conn, providers: dict | None) -> None:
    if not providers or not providers.get("tts"):
        st.warning("이 설치에 TTS 가 연결되어 있지 않습니다.")
        return

    provider = st.selectbox("Provider", providers["tts"], key="tts_provider")
    tts_models = providers.get("tts_models", {}).get(provider) or []
    model = st.selectbox("Model", tts_models, key="tts_model") if tts_models else None

    model_states = {
        m.get("model"): m.get("state") for m in (providers.get("tts_model_states", {}).get(provider) or [])
    }
    if model and model_states.get(model) not in (None, "ok"):
        st.warning(f"이 모델은 현재 사용 불가 상태입니다: {model_states.get(model)}")

    show_all = st.checkbox("전체보기 (basket 무시하고 /tts/voices 전체 조회)", key="tts_show_all")

    voice_options: list[dict] = []

    if not show_all:
        if st.session_state.get("tts_basket_cache") is None or st.button("basket 새로고침", key="tts_basket_refresh"):
            try:
                st.session_state["tts_basket_cache"] = api_client.tts_basket(conn)
            except ApiError as e:
                show_error(e)
        basket = st.session_state.get("tts_basket_cache")
        if basket:
            if not basket.get("basket") and not basket.get("voices"):
                st.info("이 서비스에 지정된 목소리 묶음이 없습니다. '전체보기' 를 사용하세요.")
            else:
                b = basket.get("basket")
                if b:
                    st.caption(f"묶음: {b.get('name')} (v{b.get('version')})")
                for v in basket.get("voices", []):
                    voice_options.append(
                        {
                            "provider": v.get("provider", provider),
                            "model": v.get("model", model),
                            "voice": v.get("voice"),
                            "display_name": v.get("display_name") or v.get("voice"),
                            "voice_mode": None,  # basket 응답엔 kind 정보가 없음
                            "state": v.get("state"),
                        }
                    )
    else:
        if model:
            cache = st.session_state.setdefault("tts_voices_cache", {})
            cache_key = f"{provider}::{model}"
            if cache_key not in cache:
                try:
                    cache[cache_key] = api_client.tts_voices(conn, model=model, provider=provider)
                except ApiError as e:
                    show_error(e)
                    cache[cache_key] = None
            data = cache.get(cache_key)
            if data:
                for v in data.get("voices", []):
                    voice_options.append(
                        {
                            "provider": data.get("provider", provider),
                            "model": model,
                            "voice": v.get("id"),
                            "display_name": v.get("display_name") or v.get("id"),
                            "voice_mode": v.get("kind"),
                            "state": v.get("state"),
                        }
                    )
        else:
            st.info("model 을 먼저 선택하세요.")

    selected = None
    if voice_options:
        labels = [f"{o['display_name']} ({o['provider']}/{o['model']})" for o in voice_options]
        idx = st.selectbox("목소리", range(len(voice_options)), format_func=lambda i: labels[i], key="tts_voice_idx")
        selected = voice_options[idx]
        if selected.get("state") not in (None, "ok"):
            st.warning(f"이 목소리는 사용 불가 상태입니다: {selected.get('state')}")

    voice_mode = selected.get("voice_mode") if selected else None
    if selected and not voice_mode:
        voice_mode = st.radio(
            "voice_mode (basket 응답엔 kind 정보가 없어 직접 선택)",
            ["preset", "clone"],
            key="tts_voice_mode_manual",
            horizontal=True,
        )

    text = st.text_area("텍스트", key="tts_text")
    use_speed = st.checkbox("speed 사용", key="tts_use_speed")
    speed = st.slider("speed", 0.5, 2.0, 1.0, key="tts_speed") if use_speed else None
    language = st.text_input("language (선택)", key="tts_language")

    with st.expander("고급"):
        instructions = st.text_input("instructions (톤 지시, 선택)", key="tts_instructions") or None
        use_seed = st.checkbox("seed 사용", key="tts_use_seed")
        seed = int(st.number_input("seed", value=0, step=1, key="tts_seed")) if use_seed else None
        output_check = st.checkbox("output_check (되전사 일치율)", key="tts_output_check")

    disabled = not text or selected is None

    col1, col2 = st.columns(2)
    with col1:
        if st.button("합성", disabled=disabled):
            try:
                audio, headers = api_client.tts_synthesize(
                    conn,
                    text=text,
                    provider=selected["provider"],
                    model=selected["model"],
                    voice=selected["voice"],
                    voice_mode=voice_mode,
                    language=language or None,
                    speed=speed,
                    instructions=instructions,
                    seed=seed,
                    output_check=output_check or None,
                )
            except ApiError as e:
                show_error(e)
            else:
                st.audio(audio, format=headers.get("Content-Type", "audio/wav"))
                st.download_button("다운로드", data=audio, file_name="synthesis.wav", key="tts_dl_sync")
                if output_check:
                    st.caption(
                        f"일치율: {headers.get('X-Match-Rate', headers.get('X-Output-Check', 'N/A'))}"
                    )
                with st.expander("응답 헤더"):
                    st.json(headers)
    with col2:
        if st.button("스트리밍 합성", disabled=disabled):
            try:
                t0 = time.perf_counter()
                chunks, headers = api_client.tts_synthesize_stream(
                    conn,
                    text=text,
                    provider=selected["provider"],
                    model=selected["model"],
                    voice=selected["voice"],
                    voice_mode=voice_mode,
                    language=language or None,
                    speed=speed,
                    instructions=instructions,
                    seed=seed,
                )
                pcm = bytearray()
                ttfb = None
                for chunk in chunks:
                    if ttfb is None:
                        ttfb = time.perf_counter() - t0
                    pcm.extend(chunk)
            except ApiError as e:
                show_error(e)
            else:
                sample_rate = int(headers.get("X-Sample-Rate", "44100"))
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sample_rate)
                    w.writeframes(bytes(pcm))
                st.audio(buf.getvalue(), format="audio/wav")
                st.download_button("다운로드 (WAV)", data=buf.getvalue(), file_name="synthesis_stream.wav", key="tts_dl_stream")
                if ttfb is not None:
                    st.caption(f"TTFB: {ttfb * 1000:.0f}ms · model: {headers.get('X-Model', '?')}")

    st.divider()
    st.subheader("실시간 (WebSocket)")

    ws_voice_mode = st.radio(
        "voice_mode (필수)",
        ["preset", "clone", "custom_voice", "voice_design", "random_voice"],
        key="tts_ws_voice_mode",
        horizontal=True,
    )
    ws_voice = st.text_input("voice", value=(selected["voice"] if selected else ""), key="tts_ws_voice")
    ws_language = st.text_input("language (필수 — supertonic3 은 auto 를 받지 않음)", value="ko", key="tts_ws_language")
    ws_provider = st.text_input(
        "provider (선택)", value=(selected.get("provider") if selected else provider) or "", key="tts_ws_provider"
    )
    ws_model = st.text_input("model (선택)", value=(selected.get("model") if selected else model) or "", key="tts_ws_model")

    if st.button("연결", key="tts_ws_connect"):
        url = ws_client.build_tts_ws_url(
            conn,
            voice_mode=ws_voice_mode,
            voice=ws_voice or None,
            language=ws_language or None,
            model=ws_model or None,
            provider=ws_provider or None,
        )
        session = ws_client.TtsWsSession(url, header=[f"X-API-Key: {conn.api_key}"])
        try:
            session.connect()
        except Exception as e:  # noqa: BLE001
            st.error(f"WS 연결 실패: {e}")
        else:
            st.session_state["tts_ws_session"] = session
            st.session_state["tts_ws_pcm"] = bytearray()
            st.session_state["tts_ws_events"] = []
            st.session_state["tts_ws_sample_rate"] = None
            st.session_state["tts_ws_fatal"] = False
            st.session_state["tts_ws_utterance_ended"] = False
            st.success("연결됨")

    session = st.session_state.get("tts_ws_session")

    if session is not None:
        for kind, payload in session.drain():
            if kind == "binary":
                st.session_state["tts_ws_pcm"].extend(payload)
            elif kind == "json":
                st.session_state.setdefault("tts_ws_events", []).append(payload)
                ev_type = payload.get("type")
                if ev_type == "ready":
                    st.session_state["tts_ws_sample_rate"] = payload.get("sample_rate")
                elif ev_type == "utterance_end":
                    st.session_state["tts_ws_utterance_ended"] = True
                elif ev_type == "error" and payload.get("fatal"):
                    st.session_state["tts_ws_fatal"] = True
            elif kind in ("closed", "error"):
                st.session_state["tts_ws_fatal"] = True

    fatal = st.session_state.get("tts_ws_fatal", False)
    if fatal:
        st.error("치명적 오류(또는 연결 종료)로 세션을 더 이상 사용할 수 없습니다. 다시 연결하세요.")

    ws_text = st.text_input("보낼 텍스트", key="tts_ws_text_input")
    c1, c2, c3, c4, c5 = st.columns(5)
    disabled_ws = session is None or fatal
    with c1:
        if st.button("text 전송", disabled=disabled_ws) and session:
            session.send_text(ws_text)
    with c2:
        if st.button("flush (발화 끝)", disabled=disabled_ws) and session:
            session.flush()
    with c3:
        if st.button("cancel (끼어들기)", disabled=disabled_ws) and session:
            session.cancel()
    with c4:
        if st.button("ping", disabled=disabled_ws) and session:
            session.ping()
    with c5:
        utterance_ended = st.session_state.get("tts_ws_utterance_ended", False)
        if st.button("end", disabled=disabled_ws or not utterance_ended) and session:
            session.end()
            st.session_state["tts_ws_session"] = None

    with st.expander("세션 중 config 변경"):
        cfg_voice = st.text_input("voice (선택)", key="tts_ws_cfg_voice")
        cfg_speed = st.number_input("speed", value=1.0, key="tts_ws_cfg_speed")
        if st.button("config 적용", disabled=disabled_ws) and session:
            kwargs = {"speed": cfg_speed}
            if cfg_voice:
                kwargs["voice"] = cfg_voice
            session.config(**kwargs)

    st.caption(
        "끼어들기 테스트 절차: 긴 문장을 text+flush 로 보내고, PCM 이 재생되는 동안 "
        "'cancel' 버튼을 눌러 이벤트 로그에 cancelled 가 오는지 확인한 뒤, "
        "새 text+flush 로 정상 합성이 재개되는지 본다."
    )

    if st.session_state.get("tts_ws_pcm"):
        sample_rate = st.session_state.get("tts_ws_sample_rate") or 44100
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(bytes(st.session_state["tts_ws_pcm"]))
        st.audio(buf.getvalue(), format="audio/wav")
        st.download_button("다운로드 (WAV)", data=buf.getvalue(), file_name="tts_ws.wav", key="tts_ws_download")

    with st.expander("이벤트 로그"):
        for ev in st.session_state.get("tts_ws_events", []):
            st.json(ev)

    if st.button("연결 종료", disabled=session is None):
        if session:
            session.close()
        st.session_state["tts_ws_session"] = None


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------

def main() -> None:
    st.title("hplatform 클라이언트 PoC")
    conn = render_sidebar()
    providers = st.session_state.get("providers")

    tab_stt, tab_llm, tab_tts = st.tabs(["STT", "LLM", "TTS"])
    with tab_stt:
        render_stt_tab(conn, providers)
    with tab_llm:
        render_llm_tab(conn, providers)
    with tab_tts:
        render_tts_tab(conn, providers)

    with st.expander("설정 반영 확인 절차 (README)"):
        st.markdown(
            "1. 콘솔에서 서비스의 LLM/STT 를 바꾼다.\n"
            "2. 이 앱에서 **'서비스 설정 그대로'** 로 호출 → 응답의 provider/model 이 방금 바꾼 값인지 본다.\n"
            "3. 허용 범위 밖 값을 **'직접 지정'** 으로 보내 403 이 나는지 본다.\n"
            "4. TTS 는 콘솔에서 묶음을 바꾸고 사이드바 '연결 확인' 을 다시 눌러 basket 이 바뀌는지 본다."
        )


if __name__ == "__main__":
    main()
