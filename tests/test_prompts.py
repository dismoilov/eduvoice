"""The prompt cache — the reason the greeting starts instantly."""

import asyncio

from eduvoice.fakes import FakeTextToSpeech
from eduvoice.prompts import PromptLibrary

TEXTS = {"greeting": "assalomu alaykum", "goodbye": "rahmat"}


async def test_a_phrase_is_synthesised_once_and_then_served_from_memory(tmp_path):
    tts = FakeTextToSpeech(first_chunk_ms=1, ms_per_char=1)
    prompts = PromptLibrary(TEXTS, tmp_path)

    first = await prompts.audio("greeting", tts)
    second = await prompts.audio("greeting", tts)

    assert first == second
    assert tts.spoken == ["assalomu alaykum"], "the phrase was synthesised twice"


async def test_two_calls_at_once_do_not_synthesise_the_same_phrase_twice(tmp_path):
    tts = FakeTextToSpeech(first_chunk_ms=20, ms_per_char=1)
    prompts = PromptLibrary(TEXTS, tmp_path)

    results = await asyncio.gather(*(prompts.audio("greeting", tts) for _ in range(5)))

    assert len({bytes(r) for r in results}) == 1
    assert tts.spoken == ["assalomu alaykum"]


async def test_the_cache_survives_a_restart(tmp_path):
    await PromptLibrary(TEXTS, tmp_path).audio("greeting", FakeTextToSpeech(first_chunk_ms=1))

    fresh_tts = FakeTextToSpeech(first_chunk_ms=1)
    pcm = await PromptLibrary(TEXTS, tmp_path).audio("greeting", fresh_tts)

    assert pcm, "nothing was read back from disk"
    assert fresh_tts.spoken == [], "a cached phrase was synthesised again after a restart"


async def test_prewarm_prepares_every_phrase(tmp_path):
    prompts = PromptLibrary(TEXTS, tmp_path)

    ready = await prompts.prewarm(FakeTextToSpeech(first_chunk_ms=1, ms_per_char=1))

    assert ready == len(TEXTS)
    assert sorted(p.name for p in tmp_path.glob("*.pcm")) == ["goodbye.pcm", "greeting.pcm"]


async def test_a_broken_provider_does_not_stop_the_service_from_starting(tmp_path):
    class BrokenTts(FakeTextToSpeech):
        async def stream(self, text, language):
            raise RuntimeError("no connection")
            yield b""  # pragma: no cover — makes this an async generator

    assert await PromptLibrary(TEXTS, tmp_path).prewarm(BrokenTts()) == 0
