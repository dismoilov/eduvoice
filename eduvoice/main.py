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
import signal

from eduvoice.audiosocket import serve
from eduvoice.brain import Brain
from eduvoice.config import settings
from eduvoice.content import Faq
from eduvoice.control_api import serve_control_api
from eduvoice.interfaces import ChatModel, SpeechToText, TextToSpeech
from eduvoice.prompts import PromptLibrary
from eduvoice.registry import CallRegistry, registry
from eduvoice.session import CallSession
from store.knowledge import PublishedKnowledge
from store.lex import LexLibrary
from store.write import CallStore

log = logging.getLogger("eduvoice")


def build_providers() -> tuple[SpeechToText, TextToSpeech, ChatModel]:
    """Speech-to-text, text-to-speech and the language model for one call."""
    if settings.provider == "voicelab":
        from eduvoice.voicelab import build_voicelab_providers

        return build_voicelab_providers(settings)

    from eduvoice.fakes import FakeChatModel, FakeSpeechToText, FakeTextToSpeech

    return FakeSpeechToText(), FakeTextToSpeech(), FakeChatModel()


# How long a stop may spend writing calls in progress to the database. systemd's
# TimeoutStopSec must be larger, or it will send SIGKILL in the middle of that.
SHUTDOWN_GRACE_S = 10.0

# Calls in progress right now, so that a stop signal can write them down before exiting.
live: set[CallSession] = set()
shutting_down = asyncio.Event()


def send_to_a_person_unless_decided(calls: CallRegistry, call_id: str) -> None:
    """After a crash the dialplan must still get an answer — but not a different one.

    A session that finished its cleanup has already decided and written the call down. A
    late exception from the socket teardown must not turn that "hangup" into "operator":
    the caller had said goodbye, and the dialplan, asking a moment later, would have sent
    them to the queue instead.
    """
    if not call_id:
        return
    record = calls.get(call_id)
    if record is not None and record.ended_at is not None:
        return
    calls.set_next(call_id, "operator")


def make_call_handler(faq: Faq, prompts: PromptLibrary, knowledge=None, store=None, laws=None):
    """Builds the AudioSocket connection handler.

    Service phrases are loaded once at start-up: a missing file must break the service
    loudly at boot, not one call in the middle of the demo. Answers, on the other hand,
    are read per call: publishing one in the CRM changes what the next caller hears.
    """

    async def handle_call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session: CallSession | None = None
        try:
            if shutting_down.is_set():
                # Already stopping: the dialplan's safe default hands this caller to a
                # person rather than to a bridge that is halfway out of the door.
                with contextlib.suppress(OSError):
                    writer.close()
                return
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
                    laws=laws,
                ),
                prompts=prompts,
                registry=registry,
                config=settings,
                store=store,
            )
            live.add(session)
            await session.run()
        except Exception:  # one broken call must not take the bridge down
            call_id = session.call_id if session else ""
            log.exception("call %s crashed", call_id or "unknown")
            send_to_a_person_unless_decided(registry, call_id)
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.aclose()
            with contextlib.suppress(OSError):
                writer.close()
        finally:
            if session is not None:
                live.discard(session)

    return handle_call


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.provider == "fake":
        log.warning("running with FAKE providers: speech and answers are placeholders")

    faq = Faq.load(settings.content_dir)
    prompts = PromptLibrary.load(
        settings.content_dir, settings.audio_dir, settings.voicelab_voice_uz
    )
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
    laws = (
        LexLibrary(settings.db_path, limit=settings.lex_clauses) if settings.lex_answers else None
    )
    if laws is not None:
        log.info(
            "regulations: %d clauses indexed (a question the FAQ misses is answered from these)",
            laws.clauses(),
        )
    handler = make_call_handler(
        faq, prompts, knowledge=knowledge, store=CallStore(settings.db_path), laws=laws
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
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # not available on every platform
            loop.add_signal_handler(signal_name, shutting_down.set)

    async with audio_server, control_server:
        await shutting_down.wait()

    # `make deploy` restarts this service, and a restart used to take every conversation
    # in progress with it: the process died at the next bytecode, so the code that writes
    # a finished call to the database — and to the JSON journal that is supposed to be its
    # backup — never ran. Ten people had been talking to us and the CRM showed nothing.
    in_progress = list(live)
    log.info("stopping: %d call(s) in progress", len(in_progress))
    if in_progress:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(*(call.aclose() for call in in_progress), return_exceptions=True),
                timeout=SHUTDOWN_GRACE_S,
            )
    log.info("stopped")


if __name__ == "__main__":
    asyncio.run(main())
