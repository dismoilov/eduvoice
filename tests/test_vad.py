from dataclasses import replace

from eduvoice.audio import silence, tone
from eduvoice.audiosocket import FRAME_BYTES
from eduvoice.config import settings as base_settings
from eduvoice.interfaces import SAMPLE_RATE_TELEPHONY
from eduvoice.vad import BargeInDetector, SpeechDetector, WebrtcSpeechTest

SPEECH = b"\x11" * FRAME_BYTES  # content does not matter: the speech test is injected
QUIET = b"\x00" * FRAME_BYTES


def scripted(*sequence: bool):
    """Speech test that replays a fixed answer per frame, then reports silence."""
    answers = iter(sequence)

    def test(_frame: bytes) -> bool:
        return next(answers, False)

    return test


def test_no_utterance_while_the_caller_is_quiet():
    detector = SpeechDetector(base_settings, speech_test=lambda _frame: False)
    for _ in range(100):
        assert detector.push(QUIET) is None
    assert not detector.in_speech
    assert detector.silence_ms == 2000


def test_phrase_ends_after_the_configured_silence():
    cfg = replace(
        base_settings, speech_start_frames=3, speech_start_window=5, silence_end_frames=35
    )
    # 3 speech frames start the phrase, 40 speech frames carry it, then silence ends it
    detector = SpeechDetector(cfg, speech_test=scripted(*([True] * 43)))

    result = None
    for i in range(43 + 40):
        result = detector.push(SPEECH if i < 43 else QUIET)
        if result:
            break

    assert result is not None
    assert result.ended_by == "silence"
    assert not detector.in_speech


def test_utterance_keeps_preroll_and_trims_trailing_silence():
    cfg = replace(
        base_settings,
        speech_start_frames=3,
        speech_start_window=5,
        silence_end_frames=10,
        preroll_ms=100,  # 5 frames
    )
    # 4 quiet frames (pre-roll), then 10 speech frames, then silence
    script = [False] * 4 + [True] * 10
    detector = SpeechDetector(cfg, speech_test=scripted(*script))

    result = None
    for i in range(4 + 10 + 12):
        result = detector.push(SPEECH if 4 <= i < 14 else QUIET)
        if result:
            break

    assert result is not None
    frames = len(result.pcm8k) // FRAME_BYTES
    # pre-roll (up to 5) + speech (10) + kept tail (5), minus the trimmed silence
    assert 15 <= frames <= 20, frames
    assert result.duration_ms == frames * 20


def test_long_monologue_is_cut_at_the_limit():
    cfg = replace(
        base_settings,
        speech_start_frames=1,
        speech_start_window=1,
        max_utterance_s=0.5,  # 25 frames
        silence_end_frames=1000,
    )
    detector = SpeechDetector(cfg, speech_test=lambda _frame: True)

    result = None
    for _ in range(60):
        result = detector.push(SPEECH)
        if result:
            break

    assert result is not None
    assert result.ended_by == "max_length"
    assert result.duration_ms <= 520


def test_barge_in_ignores_echo_right_after_playback_starts():
    cfg = replace(base_settings, barge_in_frames=15, barge_in_window=20, barge_in_guard_ms=500)
    detector = BargeInDetector(cfg, speech_test=lambda _frame: True)
    detector.playback_started()
    detector.playback_audible()

    # guard = 25 frames: everything inside it is ignored even if it looks like speech
    assert all(detector.push(SPEECH) is False for _ in range(25))
    # after the guard, 15 speech frames in a row trigger the interruption
    assert any(detector.push(SPEECH) for _ in range(15))


def test_barge_in_needs_sustained_speech_not_one_noisy_frame():
    cfg = replace(base_settings, barge_in_frames=15, barge_in_window=20, barge_in_guard_ms=0)
    detector = BargeInDetector(cfg, speech_test=scripted(True, False, True, False))
    detector.playback_started()
    detector.playback_audible()

    assert not any(detector.push(SPEECH) for _ in range(20))


def test_real_webrtcvad_does_not_hear_speech_in_silence():
    is_speech = WebrtcSpeechTest(aggressiveness=2)
    quiet = silence(20, SAMPLE_RATE_TELEPHONY)
    assert is_speech(quiet) is False


def test_real_webrtcvad_accepts_20ms_telephony_frames():
    """Frame size contract: webrtcvad only accepts 10/20/30 ms frames."""
    is_speech = WebrtcSpeechTest(aggressiveness=2)
    frame = tone(20, SAMPLE_RATE_TELEPHONY)
    assert isinstance(is_speech(frame), bool)


def test_seed_replays_the_start_of_an_interrupted_phrase():
    """Barge-in: the frames the caller spoke over the bot must reach recognition."""
    cfg = replace(base_settings, speech_start_frames=2, speech_start_window=3, silence_end_frames=5)
    detector = SpeechDetector(cfg, speech_test=lambda f: f[:1] == b"\x11")
    detector.seed([SPEECH] * 5)

    assert detector.in_speech, "seeded speech did not start an utterance"
    utterance = None
    for frame in [SPEECH] * 3 + [QUIET] * 10:
        utterance = detector.push(frame) or utterance

    assert utterance is not None
    # 5 seeded + 3 spoken frames must all be there (pre-roll may add a little more).
    assert utterance.duration_ms >= 8 * 20


def test_nobody_can_interrupt_a_phrase_that_has_not_been_played_yet():
    """The guard exists to ignore our own voice echoing back, so it starts with the voice.

    Started when the phrase was merely *requested*, it was spent on the silence while the
    answer was being synthesised — and a caller saying "hello?" into that gap cancelled an
    answer they had not heard one sample of. The synthesis is paid for, is not cached when
    it is abandoned, and the question comes round again.
    """
    cfg = replace(base_settings, barge_in_frames=3, barge_in_window=5, barge_in_guard_ms=100)
    detector = BargeInDetector(cfg, speech_test=lambda _frame: True)

    detector.playback_started()  # synthesis begins; the line is silent
    assert all(detector.push(SPEECH) is False for _ in range(50)), "interrupted silence"

    detector.playback_audible()  # the first sound reaches the caller
    assert all(detector.push(SPEECH) is False for _ in range(5)), "the guard did not restart"
    assert any(detector.push(SPEECH) for _ in range(10)), "a real interruption was ignored"
