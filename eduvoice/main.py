"""Bridge entry point: AudioSocket server + control API for the dialplan.

Providers are chosen by EDUVOICE_PROVIDER: `fake` runs the whole call flow without any API
key, `voicelab` talks to the real service through eduvoice/voicelab.py. Nothing else in
the code changes between the two.

Each call gets its own provider objects on purpose: VoiceLab's realtime sockets are
per-conversation, and one call's broken socket must not affect anybody else's call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from eduvoice.audiosocket import serve
from eduvoice.brain import Brain
from eduvoice.config import settings
from eduvoice.content import Faq
from eduvoice.control_api import serve_control_api
from eduvoice.interfaces import ChatModel, SpeechToText, TextToSpeech
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import registry
from eduvoice.session import CallSession
from store.knowledge import PublishedKnowledge
from store.write import CallStore

log = logging.getLogger("eduvoice")


def build_providers() -> tuple[SpeechToText, TextToSpeech, ChatModel]:
    """Speech-to-text, text-to-speech and the language model for one call."""
    if settings.provider == "voicelab":
        from eduvoice.voicelab import build_voicelab_providers

        return build_voicelab_providers(settings)

    from eduvoice.fakes import FakeChatModel, FakeSpeechToText, FakeTextToSpeech

    return FakeSpeechToText(), FakeTextToSpeech(), FakeChatModel()


def make_call_handler(faq: Faq, prompts: PromptLibrary, knowledge=None, store=None):
    """Builds the AudioSocket connection handler.

    Service phrases are loaded once at start-up: a missing file must break the service
    loudly at boot, not one call in the middle of the demo. Answers, on the other hand,
    are read per call: publishing one in the CRM changes what the next caller hears.
    """

    async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session: CallSession | None = None
        try:
            stt, tts, model = build_providers()
            published = knowledge.entries() if knowledge is not None else {}
            answers = Faq.from_records(published) if published else faq
            session = CallSession(
                reader,
                writer,
                stt=stt,
                tts=tts,
                brain=Brain(
                    model,
                    faq=answers,
                    timeout_s=settings.llm_timeout_s,
                    max_turns=settings.max_turns,
                ),
                prompts=prompts,
                registry=registry,
                config=settings,
                store=store,
            )
            await session.run()
        except Exception:  # one broken call must not take the bridge down
            call_id = session.call_id if session else ""
            log.exception("call %s crashed", call_id or "unknown")
            # The dialplan asks what to do next; "operator" means a human picks up.
            if call_id:
                registry.set_next(call_id, "operator")
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.aclose()
            with contextlib.suppress(OSError):
                writer.close()

    return handle_call


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.provider == "fake":
        log.warning("running with FAKE providers: speech and answers are placeholders")

    faq = Faq.load(settings.content_dir)
    prompts = PromptLibrary.load(settings.content_dir, settings.audio_dir)
    log.info("content: %d FAQ answers, greeting: %r", len(faq), prompts.text("greeting")[:40])

    # Synthesise the service phrases before the first caller arrives, so the greeting
    # starts within milliseconds instead of waiting for text-to-speech.
    _, warm_tts, _ = build_providers()
    ready = await prompts.prewarm(warm_tts)
    with contextlib.suppress(Exception):
        await warm_tts.close()
    log.info("prompt audio ready: %d of %d", ready, len(prompts.ids()))

    knowledge = PublishedKnowledge(settings.db_path)
    published = knowledge.entries()
    log.info(
        "answers: %d published in the CRM, %d in content/faq.yaml (CRM wins when not empty)",
        len(published),
        len(faq),
    )
    handler = make_call_handler(
        faq, prompts, knowledge=knowledge, store=CallStore(settings.db_path)
    )
    audio_server = await serve(handler, settings.audiosocket_host, settings.audiosocket_port)
    control_server = await serve_control_api(
        registry,
        settings.control_host,
        settings.control_port,
        health={"provider": settings.provider},
    )
    log.info(
        "bridge ready: AudioSocket %s:%d, control API %s:%d (provider=%s)",
        settings.audiosocket_host,
        settings.audiosocket_port,
        settings.control_host,
        settings.control_port,
        settings.provider,
    )
    async with audio_server, control_server:
        await asyncio.gather(audio_server.serve_forever(), control_server.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
