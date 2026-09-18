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
    # The name carries a fingerprint of the text, so an edited phrase cannot be served
    # from a stale file — see the test at the bottom of this file.
    names = sorted(p.name.split("-")[0] for p in tmp_path.glob("*.pcm"))
    assert names == ["goodbye", "greeting"]


async def test_a_broken_provider_does_not_stop_the_service_from_starting(tmp_path):
    class BrokenTts(FakeTextToSpeech):
        async def stream(self, text, language):
            raise RuntimeError("no connection")
            yield b""  # pragma: no cover — makes this an async generator

    assert await PromptLibrary(TEXTS, tmp_path).prewarm(BrokenTts()) == 0


async def test_editing_a_phrase_changes_what_the_caller_hears(tmp_path):
    """A cache keyed by phrase id alone would keep playing the old wording forever.

    That is worse than it sounds: the supervisor edits the text, the CRM then shows the
    new wording beside a recording of the old one, and `make deploy` deliberately keeps
    the audio directory — so nothing short of deleting files by hand would ever fix it.
    """
    from eduvoice.fakes import FakeTextToSpeech
    from eduvoice.prompts import PromptLibrary

    tts = FakeTextToSpeech(first_chunk_ms=1, ms_per_char=1)
    before = await PromptLibrary({"greeting": "eski salom"}, tmp_path).audio("greeting", tts)

    await PromptLibrary({"greeting": "yangi salom"}, tmp_path).audio("greeting", tts)

    assert tts.spoken == ["eski salom", "yangi salom"], "the edit was served from the old file"
    # Both files stay: this is paid-for audio, and going back to the old wording is free.
    assert len(list(tmp_path.glob("*.pcm"))) == 2
    again = await PromptLibrary({"greeting": "eski salom"}, tmp_path).audio("greeting", tts)
    assert again == before and len(tts.spoken) == 2, "reverting must not cost another synthesis"
