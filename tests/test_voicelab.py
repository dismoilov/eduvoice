"""The VoiceLab providers, driven against a stand-in service.

No key and no network are needed here: a fake transport answers exactly the way the real
API answered when it was probed, including the part that surprised us — recognition is
queued and its result has to be collected.
"""

import io
import json
import wave

import httpx
import pytest

from eduvoice.interfaces import ChatMessage, LlmError, SttError, TtsError
from eduvoice.voicelab import (
    VoiceLabChatModel,
    VoiceLabSpeechToText,
    VoiceLabTextToSpeech,
    _as_json,
)

PCM = b"\x01\x02" * 8000  # a second of 16 kHz audio


def wav_bytes(seconds: float = 0.5, rate: int = 24000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x03\x04" * int(rate * seconds))
    return buffer.getvalue()


def service(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://api.voicelab.uz", transport=httpx.MockTransport(handler)
    )


# ------------------------------------------------------------------ recognition


async def test_a_queued_transcription_is_collected(monkeypatch):
    """The real service answers 202 and hands over a job id; the text comes later."""
    asked = {"posts": 0, "polls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            asked["posts"] += 1
            assert request.headers.get("idempotency-key"), "every upload needs its own key"
            return httpx.Response(202, json={"id": "stt_1", "status": "queued"})
        asked["polls"] += 1
        if asked["polls"] < 2:
            return httpx.Response(200, json={"id": "stt_1", "status": "processing"})
        return httpx.Response(
            200,
            json={
                "id": "stt_1",
                "status": "completed",
                "transcript": " stipendiya qanday olinadi ",
                "duration_ms": 1700,
            },
        )

    stt = VoiceLabSpeechToText("key", timeout_s=5)
    stt._client = service(handler)

    transcript = await stt.transcribe(PCM)

    assert transcript.text == "stipendiya qanday olinadi", "the text is trimmed, not padded"
    assert transcript.duration_ms == 1700
    assert asked == {"posts": 1, "polls": 2}
    await stt.close()


async def test_silence_is_not_an_error():
    """A caller who said nothing must be asked to repeat, not sent to an operator."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "stt_2"})
        return httpx.Response(
            200,
            json={
                "id": "stt_2",
                "status": "failed",
                "error": {"code": "no_speech_detected"},
                "message": "No speech was detected.",
            },
        )

    stt = VoiceLabSpeechToText("key", timeout_s=5)
    stt._client = service(handler)

    assert (await stt.transcribe(PCM)).text == ""
    await stt.close()


async def test_a_refused_upload_becomes_an_stt_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"message": "bad key", "error": {"code": "unauthorized"}, "request_id": "req_9"},
        )

    stt = VoiceLabSpeechToText("key", timeout_s=5)
    stt._client = service(handler)

    with pytest.raises(SttError) as failure:
        await stt.transcribe(PCM)
    assert "req_9" in str(failure.value), "the id support will ask for must be in the message"
    await stt.close()


async def test_recognition_that_never_finishes_gives_up():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(202, json={"id": "stt_3"})
        return httpx.Response(200, json={"status": "processing"})

    stt = VoiceLabSpeechToText("key", timeout_s=0.6)
    stt._client = service(handler)

    with pytest.raises(SttError):
        await stt.transcribe(PCM)
    await stt.close()


# ------------------------------------------------------------------- synthesis


async def test_synthesis_streams_raw_pcm_and_caches_it(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        assert body["voice_id"] == "voice_1" and body["language"] == "uz"
        return httpx.Response(200, content=wav_bytes(), headers={"content-type": "audio/wav"})

    speech = VoiceLabTextToSpeech("key", "voice_1", cache_dir=tmp_path)
    speech._client = service(handler)

    first = b"".join([chunk async for chunk in speech.stream("Assalomu alaykum", "uz")])
    second = b"".join([chunk async for chunk in speech.stream("Assalomu alaykum", "uz")])

    assert first[:4] != b"RIFF", "a WAV header inside the stream would be heard as a click"
    assert first == second
    assert calls["n"] == 1, "the second time it must come from the cache"
    assert len(list(tmp_path.glob("*.pcm"))) == 1
    await speech.close()


async def test_synthesis_without_a_voice_fails_clearly(tmp_path):
    speech = VoiceLabTextToSpeech("key", "", cache_dir=tmp_path)
    speech._client = service(lambda r: httpx.Response(200, content=wav_bytes()))

    with pytest.raises(TtsError):
        [chunk async for chunk in speech.stream("salom", "uz")]
    await speech.close()


async def test_a_refused_synthesis_becomes_a_tts_error(tmp_path):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422, json={"error": {"code": "validation_error", "message": "Choose a valid voice."}}
        )

    speech = VoiceLabTextToSpeech("key", "voice_1", cache_dir=tmp_path)
    speech._client = service(handler)

    with pytest.raises(TtsError):
        [chunk async for chunk in speech.stream("salom", "uz")]
    await speech.close()


# ----------------------------------------------------------------------- model


@pytest.mark.parametrize(
    "content, expected",
    [
        ('{"intent": "operator"}', {"intent": "operator"}),
        (
            '```json\n{"intent": "faq", "faq_id": "stipend"}\n```',
            {"intent": "faq", "faq_id": "stipend"},
        ),
        ('Mana javob: {"intent": "goodbye"} rahmat', {"intent": "goodbye"}),
    ],
)
def test_the_models_json_is_found_even_when_it_is_wrapped(content, expected):
    assert _as_json(content) == expected


@pytest.mark.parametrize("content", ["salom", "[1, 2]", "{broken"])
def test_an_answer_that_is_not_json_is_an_error(content):
    with pytest.raises(LlmError):
        _as_json(content)


async def test_the_model_reply_is_parsed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"intent": "faq", "faq_id": "stipend"}'}}]},
        )

    # The client is built before patching, or `service` would call the patched class.
    prepared = service(handler)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: prepared)
    model = VoiceLabChatModel("key")

    answer = await model.complete_json(
        [ChatMessage("system", "rules"), ChatMessage("user", "stipendiya")], timeout_s=5
    )

    assert answer == {"intent": "faq", "faq_id": "stipend"}


async def test_speech_at_the_wrong_sample_rate_is_refused_not_cached(tmp_path):
    """The bridge resamples 24 kHz to the telephone's 8 kHz without asking.

    So a reply at any other rate plays at the wrong speed — and, left unchecked, was
    written into the cache under the text's hash and replayed to every later caller. An
    error here sends this one caller to a person; a cached file would spoil the answer
    for good.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=wav_bytes(rate=16000), headers={"content-type": "audio/wav"}
        )

    speech = VoiceLabTextToSpeech("key", "voice_1", cache_dir=tmp_path)
    speech._client = service(handler)

    with pytest.raises(TtsError) as failure:
        [chunk async for chunk in speech.stream("salom", "uz")]
    assert "16000" in str(failure.value)
    assert list(tmp_path.glob("*.pcm")) == [], "broken audio must not reach the cache"
    await speech.close()


async def test_an_answer_that_is_not_a_json_object_is_a_clean_error():
    """A proxy or a captive portal can answer with an array, or with HTML.

    `body.get(...)` on that raises AttributeError, which is not one of the three error
    types the bridge knows how to turn into "a person will help you".
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    stt = VoiceLabSpeechToText("key", timeout_s=2)
    stt._client = service(handler)
    with pytest.raises(SttError):
        await stt.transcribe(PCM)
    await stt.close()

    model = VoiceLabChatModel("key")
    model._client = service(handler)
    with pytest.raises(LlmError):
        await model.complete_json([ChatMessage("user", "salom")], timeout_s=2)
    await model.close()
