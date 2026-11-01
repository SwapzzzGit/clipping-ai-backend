import copy

import pytest

from clipper.config import DEFAULTS


@pytest.fixture
def cfg():
    return copy.deepcopy(DEFAULTS)


def make_segments(sentences, sec_per_word=0.4, gap=0.1, start=0.0):
    """Build a word-timed transcript; each sentence becomes one caption segment."""
    segs, t = [], start
    for sentence in sentences:
        words = []
        for tok in sentence.split():
            words.append({"start": round(t, 3), "end": round(t + sec_per_word, 3), "text": tok})
            t += sec_per_word
        segs.append({"start_sec": words[0]["start"], "end_sec": words[-1]["end"], "text": sentence, "words": words})
        t += gap
    return segs


@pytest.fixture
def long_segments():
    # ~6 minutes of numbered sentences, 8 words (3.2 s) each.
    return make_segments([f"Sentence number {i} says something rather interesting here." for i in range(110)])
