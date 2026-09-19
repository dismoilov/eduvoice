"""Settings: everything tunable lives here and comes from the environment (.env)."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from eduvoice.interfaces import Language

# eduvoice/config.py -> repository root (flat layout: eduvoice/ sits in the root)
ROOT = Path(__file__).resolve().parents[1]

load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass(slots=True)
class Settings:
    # --- network ---------------------------------------------------------
    audiosocket_host: str = os.getenv("BRIDGE_HOST", "127.0.0.1")
    audiosocket_port: int = _int("BRIDGE_PORT", 9092)
    control_host: str = os.getenv("CONTROL_HOST", "127.0.0.1")
    control_port: int = _int("CONTROL_PORT", 9093)

    # --- providers: "fake" until the VoiceLab module is ready -------------
    provider: Literal["fake", "voicelab"] = os.getenv("EDUVOICE_PROVIDER", "fake")  # type: ignore[assignment]
    voicelab_api_key: str = os.getenv("VOICELAB_API_KEY", "")
    voicelab_voice_uz: str = os.getenv("VOICELAB_VOICE_UZ", "")
    # Slightly faster than default: on the phone 1.0 sounds sleepy.
    voicelab_speed: float = _float("VOICELAB_SPEED", 1.1)

    # --- speech detection (tuned on real calls, see TASKS.md A13) ---------
    language: Language = "uz"  # Uzbek-only service
    # 3, the strictest. Measured on the caller's own leg of a real call from the venue:
    # at 2 the crowd murmur is called speech on 70 % of frames — including 56 % of frames
    # quieter than RMS 100 — so 700 ms of silence never occurs and a phrase can only end
    # by hitting the length limit. At 3 that falls to 13 % and phrases end properly.
    vad_aggressiveness: int = _int("VAD_AGGRESSIVENESS", 3)
    speech_start_frames: int = _int("SPEECH_START_FRAMES", 3)  # of 5 frames = ~60 ms
    speech_start_window: int = _int("SPEECH_START_WINDOW", 5)
    silence_end_frames: int = _int("SILENCE_END_FRAMES", 35)  # 700 ms of silence ends a phrase
    barge_in_frames: int = _int("BARGE_IN_FRAMES", 15)  # of 20 frames = ~300 ms
    barge_in_window: int = _int("BARGE_IN_WINDOW", 20)
    barge_in_guard_ms: int = _int(
        "BARGE_IN_GUARD_MS", 500
    )  # ignore echo right after we start talking
    # How much louder than the room a frame must be to count as the caller talking.
    # 1.0 turns the test off and trusts the voice detector alone, which is right on a
    # quiet line and useless in a hall. Measured at the venue: room RMS ~3400.
    # A frame counts as the caller talking when it is above BOTH of these: a fixed level
    # (`speech_floor`) and a step above the room (`loudness_margin` x the room's level).
    # 300 measured on the caller's own leg of a real call from the venue: the room sits at
    # RMS 68 and the questions at 4400-8800, so 300 separates them cleanly while a quiet
    # test line, whose speech is around 360, still clears it. The margin only matters in a
    # room louder than 150 — there the fixed level would be deaf, and a step above the
    # room is what is wanted. Multiplying alone runs away: at ten times, a room at 3400
    # asks for 34 000, which no 16-bit sample can reach.
    speech_floor: float = _float("SPEECH_FLOOR", 300.0)
    loudness_margin: float = _float("LOUDNESS_MARGIN", 2.0)
    # Interrupting the assistant needs the same evidence as starting a phrase: measured
    # at the venue, anything gentler let the room itself cut the answer off within two
    # seconds, and nothing harsher was needed — the caller still interrupts 0.3-0.5 s
    # after they start speaking, on both the hall line and a clean one.
    barge_in_margin: float = _float("BARGE_IN_MARGIN", 2.0)
    # Whether the caller may talk over the greeting. Off: a first-time caller must hear
    # what the service is, and a noisy room interrupts it before they hear anything.
    interruptible_greeting: bool = os.getenv("INTERRUPTIBLE_GREETING", "") == "1"
    preroll_ms: int = _int("PREROLL_MS", 300)  # audio kept before speech starts
    # How much of what the caller says while the assistant is talking is carried over to
    # the moment it stops. Longer than the greeting (8.8 s), or someone who answers it in
    # its first seconds has already fallen out of the buffer by the time it ends.
    carry_over_s: float = _float("CARRY_OVER_S", 10.0)
    # A phrase this long is sent for recognition even if the noise never lets it end.
    # Better a recognition that fails — and, after three, a person — than a caller
    # talking into a system that is still waiting for a silence that will not come.
    max_utterance_s: float = _float("MAX_UTTERANCE_S", 12.0)

    # --- dialogue timing --------------------------------------------------
    silence_reprompt_s: float = _float("SILENCE_REPROMPT_S", 6.0)
    filler_after_s: float = _float("FILLER_AFTER_S", 1.2)
    # Recognition is queued on the provider's side and normally completes in 1.4–2.0 s,
    # but its tail is longer: a job was still "processing" at 5 s on a live call and the
    # caller was told there was a technical problem. The filler covers the wait from
    # 1.2 s, so a longer budget costs nothing a caller notices; a false "technical
    # problem" costs the whole conversation.
    stt_timeout_s: float = _float("STT_TIMEOUT_S", 9.0)
    llm_timeout_s: float = _float("LLM_TIMEOUT_S", 5.0)
    tts_first_chunk_timeout_s: float = _float("TTS_FIRST_CHUNK_TIMEOUT_S", 3.0)
    max_turns: int = _int("MAX_TURNS", 8)
    max_call_s: float = _float("MAX_CALL_S", 360.0)
    # Answering from the regulations themselves when the FAQ has nothing. Costs a second
    # model call, and only on the turn that would otherwise have been "I did not
    # understand you". LEX_ANSWERS=0 turns it off and the assistant behaves as before.
    lex_answers: bool = os.getenv("LEX_ANSWERS", "1") != "0"
    lex_clauses: int = _int("LEX_CLAUSES", 3)
    # Writes the audio the bridge receives to logs/<call-id>.raw — raw 8 kHz PCM. For
    # working out why a particular line is not understood; off unless asked for.
    dump_audio: bool = os.getenv("EDUVOICE_DUMP_AUDIO", "") == "1"

    # --- files ------------------------------------------------------------
    audio_dir: Path = field(
        default_factory=lambda: Path(os.getenv("AUDIO_DIR", str(ROOT / "audio")))
    )
    content_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CONTENT_DIR", str(ROOT / "content")))
    )
    call_log_dir: Path = field(
        default_factory=lambda: Path(os.getenv("CALL_LOG_DIR", str(ROOT / "logs")))
    )
    # The shared database: the bridge writes calls here, the CRM reads and edits them.
    db_path: Path = field(
        default_factory=lambda: Path(os.getenv("EDUVOICE_DB", str(ROOT / "data" / "eduvoice.db")))
    )
    # Where Asterisk's MixMonitor writes call recordings; the CRM serves them from here.
    recordings_dir: Path = field(
        default_factory=lambda: Path(os.getenv("RECORDINGS_DIR", "/var/spool/asterisk/monitor"))
    )
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()
