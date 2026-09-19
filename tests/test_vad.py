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


def _noise(rms: float, seed: int = 7) -> bytes:
    import numpy as np

    return (np.random.default_rng(seed).standard_normal(160) * rms).astype(np.int16).tobytes()


def test_the_caller_is_heard_over_the_room_in_every_room_measured():
    """A frame counts as the caller when it clears a fixed level *or* a step above the
    room, whichever is higher.

    Multiplying the room's level alone runs away: at the tenfold margin shipped for an
    hour today, a room at RMS 3400 asked for 34 000, which no 16-bit sample can reach —
    nobody would have been heard at all, and `_check_silence` ends in a goodbye, not an
    operator. A fixed level alone is deaf the other way, in a room louder than itself.

    The pairs below are measurements: the venue's own leg (room 14-68, questions
    4400-8800), a clean test line, and the two loud rooms that broke the ratio.
    """
    from eduvoice.vad import LoudnessGate

    for room, voice in ((14, 4400), (68, 8800), (0, 362), (350, 2100), (3400, 9000)):
        gate = LoudnessGate(
            base_settings.loudness_margin, floor_at_least=base_settings.speech_floor
        )
        for _ in range(120):
            gate(_noise(room))
        assert gate(_noise(voice, seed=9)) is True, f"unheard: room {room}, voice {voice}"


def test_the_room_alone_never_counts_as_speech():
    from eduvoice.vad import LoudnessGate

    for room in (14, 68, 200, 3400):
        gate = LoudnessGate(
            base_settings.loudness_margin, floor_at_least=base_settings.speech_floor
        )
        heard = [gate(_noise(room, seed=i)) for i in range(120)]
        assert not any(heard[20:]), f"a room at RMS {room} was taken for the caller"


def test_a_question_asked_over_the_greeting_is_handed_back_not_dropped():
    """`seed` replays what the caller said while the assistant was talking. A question
    that *finished* during that replay used to be returned by `push` and ignored, so
    somebody who asked in full over the greeting was answered with "say your question".
    """
    import numpy as np

    from eduvoice.vad import SpeechDetector

    rng = np.random.default_rng(11)
    loud = (rng.standard_normal(160) * 2500).astype(np.int16).tobytes()
    quiet = (rng.standard_normal(160) * 20).astype(np.int16).tobytes()
    detector = SpeechDetector(base_settings)

    spoken = detector.seed([quiet] * 20 + [loud] * 60 + [quiet] * 60)

    assert spoken, "a question finished during the replay and was thrown away"
    assert spoken[-1].duration_ms > 800


def test_waiting_for_the_assistant_to_stop_is_not_the_caller_s_silence():
    """The reprompt counts how long the caller has been quiet. Counting the seconds they
    spent waiting for a long greeting made it fire the instant the greeting ended."""
    import numpy as np

    from eduvoice.vad import SpeechDetector

    quiet = (np.random.default_rng(5).standard_normal(160) * 20).astype(np.int16).tobytes()
    detector = SpeechDetector(base_settings)

    detector.seed([quiet] * 400)  # eight seconds of greeting, caller politely waiting

    assert detector.silence_ms == 0, f"{detector.silence_ms:.0f} ms counted against them"


def test_the_room_level_is_the_tenth_percentile_not_the_lowest_frame():
    """The first frame of a call arrives before any room audio does — measured at 149
    against a hall at 4000. Taken as the minimum, that one frame set the bar for the next
    two seconds and the room walked over it, cutting the answer off half a second in."""
    from eduvoice.vad import LoudnessGate

    gate = LoudnessGate(base_settings.loudness_margin, floor_at_least=1.0)
    gate(_noise(1))  # the moment of connection, before the room is heard
    for _ in range(90):  # leaves that frame inside the window when the bar is read
        gate(_noise(2000))

    assert gate(_noise(2500)) is False, "one silent frame set the bar for the whole window"


def test_a_fixed_level_alone_would_be_deaf_in_a_loud_room():
    """Both halves of the threshold earn their place: without the margin, a room louder
    than the fixed level is heard as speech from end to end."""
    from eduvoice.vad import LoudnessGate

    gate = LoudnessGate(base_settings.loudness_margin, floor_at_least=base_settings.speech_floor)
    heard = [gate(_noise(1500, seed=i)) for i in range(120)]

    assert not any(heard[20:]), "a room above the fixed level was taken for the caller"


def test_a_step_above_the_room_alone_would_hear_a_rustle_on_a_silent_line():
    """The other half. On a near-silent line a ratio collapses onto nothing: twice a room
    at RMS 14 is 28, and a chair moving clears it. The fixed level is what a caller has to
    reach before anything is listened to at all."""
    from eduvoice.vad import LoudnessGate

    gate = LoudnessGate(base_settings.loudness_margin, floor_at_least=base_settings.speech_floor)
    for _ in range(120):
        gate(_noise(14))

    assert gate(_noise(120)) is False, "a rustle on a quiet line was taken for the caller"
    assert gate(_noise(4000)) is True, "and the caller must still be heard"


def test_what_was_said_a_moment_ago_is_put_in_front_of_the_phrase_in_progress():
    """A question split by a pause is joined back into one utterance: the first half is
    prepended, and the finished phrase carries both halves in the order they were said."""
    cfg = replace(base_settings, speech_start_frames=2, speech_start_window=3, silence_end_frames=5)
    detector = SpeechDetector(cfg, speech_test=lambda f: f[:1] == b"\x11")
    first_half = b"\x11\x01" * 1600  # 200 ms marked so it can be found again

    for frame in [SPEECH] * 3:
        detector.push(frame)
    assert detector.in_speech
    detector.prepend(first_half)
    utterance = None
    for frame in [SPEECH] * 3 + [QUIET] * 10:
        utterance = detector.push(frame) or utterance

    assert utterance is not None
    assert utterance.pcm8k.startswith(first_half), "the first half is not in front"
    assert utterance.duration_ms >= 200 + 6 * 20, "the second half was lost in the join"


def test_a_fragment_is_judged_by_its_speech_not_by_the_audio_around_it():
    """Nine hundred milliseconds of pre-roll make a door slam over a second long. What the
    detector reports as speech is the slam itself: the pre-roll in front of it and the
    silence behind it are audio, not words."""
    cfg = replace(
        base_settings,
        preroll_ms=900,
        speech_start_frames=2,
        speech_start_window=3,
        silence_end_frames=5,
    )
    detector = SpeechDetector(cfg, speech_test=lambda f: f[:1] == b"\x11")

    utterance = None
    for frame in [QUIET] * 45 + [SPEECH] * 3 + [QUIET] * 10:
        utterance = detector.push(frame) or utterance

    assert utterance is not None
    assert utterance.duration_ms > 600, "the pre-roll is missing from the audio"
    assert utterance.speech_ms <= 3 * 20, f"{utterance.speech_ms} ms counted as speech"
