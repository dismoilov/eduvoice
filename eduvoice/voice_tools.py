"""Command line helpers around VoiceLab: list voices, synthesise a file, build the IVR menu.

    uv run python -m eduvoice.voice_tools voices
    uv run python -m eduvoice.voice_tools say "Assalomu alaykum" salom.wav
    uv run python -m eduvoice.voice_tools ivr menu.wav

Asterisk plays 8 kHz mono WAV, so anything meant for the dialplan is converted here and
not on the server: the bridge's own prompts stay 24 kHz and are resampled per call.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import wave
from pathlib import Path

import httpx

from eduvoice.audio import resample
from eduvoice.config import settings
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY, SAMPLE_RATE_TTS
from eduvoice.prompts import PromptLibrary
from eduvoice.voicelab import BASE_URL, VoiceLabTextToSpeech


def list_voices(_args: argparse.Namespace) -> int:
    """Needs the `voices:read` permission on the key."""
    response = httpx.get(
        f"{BASE_URL}/v1/voices",
        headers={"Authorization": f"Bearer {settings.voicelab_api_key}"},
        params={"language": "uz"},
        timeout=20,
    )
    if response.status_code != 200:
        print(f"HTTP {response.status_code}: {response.text[:300]}")
        return 1
    for voice in response.json().get("data", []):
        print(f"  {voice.get('id')}  {voice.get('display_name')}  [{voice.get('language')}]")
    return 0


async def _synthesise(text: str) -> bytes:
    """Returns 24 kHz PCM for the text, through the same cache the bridge uses."""
    speech = VoiceLabTextToSpeech(
        settings.voicelab_api_key,
        settings.voicelab_voice_uz,
        cache_dir=settings.audio_dir / "tts-cache",
    )
    await speech.warm_up("uz")
    try:
        return b"".join([chunk async for chunk in speech.stream(text, "uz")])
    finally:
        await speech.close()


def _write_telephony_wav(path: Path, pcm24k: bytes) -> None:
    pcm8k = resample(pcm24k, SAMPLE_RATE_TTS, SAMPLE_RATE_TELEPHONY)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE_TELEPHONY)
        handle.writeframes(pcm8k)
    seconds = len(pcm8k) / (SAMPLE_RATE_TELEPHONY * 2)
    print(f"  {path}  {seconds:.1f} s, 8 kHz mono")


def say(args: argparse.Namespace) -> int:
    _write_telephony_wav(Path(args.out), asyncio.run(_synthesise(args.text)))
    return 0


def ivr(args: argparse.Namespace) -> int:
    """The menu Asterisk plays before the assistant picks up."""
    prompts = PromptLibrary.load(settings.content_dir, settings.audio_dir)
    text = prompts.text("ivr_menu")
    print(f"  matn: {text}")
    _write_telephony_wav(Path(args.out), asyncio.run(_synthesise(text)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eduvoice.voice_tools")
    commands = parser.add_subparsers(dest="command", required=True)

    voices = commands.add_parser("voices", help="list the Uzbek voices of the account")
    voices.set_defaults(run=list_voices)

    speak = commands.add_parser("say", help="synthesise a phrase into an 8 kHz WAV")
    speak.add_argument("text")
    speak.add_argument("out")
    speak.set_defaults(run=say)

    menu = commands.add_parser("ivr", help="build the IVR menu from content/prompts.yaml")
    menu.add_argument("out", nargs="?", default="audio/eduvoice-ivr-menu.wav")
    menu.set_defaults(run=ivr)

    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
