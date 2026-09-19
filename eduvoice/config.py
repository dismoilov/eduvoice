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
    vad_aggressiveness: int = _int("VAD_AGGRESSIVENESS", 2)
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
    loudness_margin: float = _float("LOUDNESS_MARGIN", 1.8)
    # Whether the caller may talk over the greeting. Off: a first-time caller must hear
    # what the service is, and a noisy room interrupts it before they hear anything.
    interruptible_greeting: bool = os.getenv("INTERRUPTIBLE_GREETING", "") == "1"
    preroll_ms: int = _int("PREROLL_MS", 300)  # audio kept before speech starts
    # A phrase this long is sent for recognition even if the noise never lets it end.
    # Better a recognition that fails — and, after three, a person — than a caller
    # talking into a system that is still waiting for a silence that will not come.
    max_utterance_s: float = _float("MAX_UTTERANCE_S", 12.0)

    # --- dialogue timing --------------------------------------------------
    silence_reprompt_s: float = _float("SILENCE_REPROMPT_S", 6.0)
    filler_after_s: float = _float("FILLER_AFTER_S", 1.2)
    stt_timeout_s: float = _float("STT_TIMEOUT_S", 5.0)
    llm_timeout_s: float = _float("LLM_TIMEOUT_S", 5.0)
    tts_first_chunk_timeout_s: float = _float("TTS_FIRST_CHUNK_TIMEOUT_S", 3.0)
    max_turns: int = _int("MAX_TURNS", 8)
    max_call_s: float = _float("MAX_CALL_S", 360.0)
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
