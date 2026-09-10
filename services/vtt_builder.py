SENTENCE_ENDINGS = (".", "!", "?", "؟", "۔")
MAX_CUE_CHARS = 80
SILENCE_GAP = 0.8


def fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def build_vtt(words: list[dict]) -> str:
    """Converts a flat [{word, start, end}] list into WebVTT karaoke format.

    Cues are split on: silence gap > 0.8s, accumulated text > 80 chars,
    or sentence-ending punctuation (. ! ? ؟ ۔).
    """
    lines: list[str] = ["WEBVTT", ""]

    cue_words: list[dict] = []
    cue_start: float | None = None
    cue_char_count = 0

    def flush() -> None:
        nonlocal cue_words, cue_start, cue_char_count
        if not cue_words or cue_start is None:
            cue_words, cue_start, cue_char_count = [], None, 0
            return
        cue_end = cue_words[-1]["end"]
        stamps = " ".join(
            f"<{fmt_ts(w['start'])}><c>{w['word']}</c>" for w in cue_words
        )
        lines.append(f"{fmt_ts(cue_start)} --> {fmt_ts(cue_end)}")
        lines.append(stamps)
        lines.append("")
        cue_words, cue_start, cue_char_count = [], None, 0

    prev_end: float | None = None
    for w in words:
        text = (w.get("word") or "").strip()
        if not text:
            continue

        # Rule 1: silence gap between words.
        gap = (w["start"] - prev_end) if prev_end is not None else 0
        if gap > SILENCE_GAP and cue_words:
            flush()

        if cue_start is None:
            cue_start = w["start"]

        cue_words.append({"word": text, "start": w["start"], "end": w["end"]})
        cue_char_count += len(text) + 1
        prev_end = w["end"]

        # Rule 2: accumulated length.
        if cue_char_count > MAX_CUE_CHARS:
            flush()
            continue

        # Rule 3: sentence-ending punctuation.
        if text.endswith(SENTENCE_ENDINGS):
            flush()

    flush()
    return "\n".join(lines)
