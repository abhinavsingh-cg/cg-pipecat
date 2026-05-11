from __future__ import annotations

import sys

from voicebot.state.assistant_speech import AssistantSpeechTracker, spoken_text_prefix


def test_spoken_text_prefix_word_boundary() -> None:
    text = "नमस्ते मैं आपकी मदद कर रही हूँ"
    prefix = spoken_text_prefix(text, 0.5)
    assert prefix
    assert prefix != text
    assert prefix.endswith((" ", "मैं", "आपकी", "मदद", "कर", "रही", "हूँ")) or prefix in text


def test_partial_publish_and_final_flush() -> None:
    tracker = AssistantSpeechTracker(frame_batch=20)
    full_text = "नमस्ते मैं आपकी मदद कर रही हूँ और अभी ऑफर बता रही हूँ"
    tracker.start_tts_utterance(full_text)

    for _ in range(40):
        tracker.note_generated_audio_frame()

    partial = None
    for _ in range(20):
        partial = tracker.note_delivered_audio_frame() or partial

    assert partial
    assert partial != full_text

    for _ in range(5):
        tracker.note_delivered_audio_frame()

    flushed = tracker.flush_current()
    assert flushed
    assert len(flushed) >= len(partial)
    assert flushed != full_text

    tracker.finish_current_utterance()


def test_complete_utterance_flushes_full_text() -> None:
    tracker = AssistantSpeechTracker(frame_batch=20)
    full_text = "जी, मैं पूरा संदेश बोल चुकी हूँ"
    tracker.start_llm_response()
    tracker.add_text_chunk(full_text)
    tracker.finish_llm_response()

    published = None
    for _ in range(30):
        tracker.note_generated_audio_frame()
        published = tracker.note_delivered_audio_frame() or published

    assert published == full_text
    assert tracker.flush_current() in (None, full_text)


def test_queued_utterances_do_not_overwrite_current_audio() -> None:
    tracker = AssistantSpeechTracker(frame_batch=20)
    greeting = "नमस्ते मैं स्वागत संदेश बोल रही हूँ"
    reply = "जी बताइए मैं सुन रही हूँ"

    tracker.start_tts_utterance(greeting)
    tracker.start_llm_response()
    tracker.add_text_chunk(reply)
    tracker.finish_llm_response()

    greeting_published = None
    for _ in range(30):
        tracker.note_generated_audio_frame()
        greeting_published = tracker.note_delivered_audio_frame() or greeting_published

    assert greeting_published == greeting
    assert tracker.flush_current() in (None, greeting)
    tracker.finish_current_utterance()

    reply_published = None
    for _ in range(30):
        tracker.note_generated_audio_frame()
        reply_published = tracker.note_delivered_audio_frame() or reply_published

    assert reply_published == reply
    assert tracker.flush_current() in (None, reply)


def main() -> int:
    try:
        test_spoken_text_prefix_word_boundary()
        test_partial_publish_and_final_flush()
        test_complete_utterance_flushes_full_text()
        test_queued_utterances_do_not_overwrite_current_audio()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("assistant speech tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
