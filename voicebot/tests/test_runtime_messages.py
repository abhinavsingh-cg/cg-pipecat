from __future__ import annotations

import sys

from voicebot.prompts.runtime_messages import (
    ARE_YOU_THERE_TEXT,
    REPEAT_MESSAGE,
    localized_runtime_message,
    normalize_runtime_language,
)


def test_repeat_message_uses_detected_language_name() -> None:
    assert localized_runtime_message(REPEAT_MESSAGE, "hindi") == (
        "माफ कीजिए, क्या आप एक बार फिर से बोल सकते हैं?"
    )


def test_repeat_message_accepts_iso_language_code() -> None:
    assert normalize_runtime_language("hi") == "hindi"
    assert localized_runtime_message(REPEAT_MESSAGE, "hi") == (
        "माफ कीजिए, क्या आप एक बार फिर से बोल सकते हैं?"
    )


def test_are_you_there_message_falls_back_to_english() -> None:
    assert localized_runtime_message(ARE_YOU_THERE_TEXT, "unknown") == "Hello, are you there?"
def main() -> int:
    try:
        test_repeat_message_uses_detected_language_name()
        test_repeat_message_accepts_iso_language_code()
        test_are_you_there_message_falls_back_to_english()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("runtime message tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
