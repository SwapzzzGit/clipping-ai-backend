import pytest

from clipper.candidates import boundaries, fit_clip, split_rules, words_text
from clipper.render import tail_end
from clipper.transcript import clean_segments

CFG = {"clips": {"min_sec": 20, "max_sec": 60, "snap_tolerance_sec": 4}}


def bounds(strong_end, weak_end=(), word_end=()):
    return {"strong_start": [0.0], "strong_end": list(strong_end), "weak_start": [], "weak_end": list(weak_end),
            "word_start": [], "word_end": list(word_end)}


def test_a_clip_ends_on_a_sentence_end_never_mid_sentence():
    b = bounds(strong_end=[40.0, 50.0], weak_end=[44.0], word_end=[44.5])
    assert fit_clip(0.0, 45.0, b, CFG)[1] in (40.0, 50.0)  # not the pause or the word end at 44
    assert fit_clip(0.0, 45.0, bounds(strong_end=[10.0]), CFG) is None  # no sentence end fits: drop the pick


def test_rules_split_into_hard_limits_and_preferences():
    hard, preferred = split_rules("DON'T pick politics.\nDO prefer actionable takeaways.\nNo graphic violence.")
    assert hard == "DON'T pick politics.\nNo graphic violence."
    assert preferred == "DO prefer actionable takeaways."


def test_caption_arrows_sound_tags_and_repeats_are_cleaned():
    segs = [
        {"start_sec": 0, "end_sec": 2, "text": ">> Okay.", "words": []},
        {"start_sec": 2, "end_sec": 4, "text": "In this study we were able", "words": []},
        {"start_sec": 4, "end_sec": 6, "text": "were able to see and by", "words": []},
        {"start_sec": 6, "end_sec": 9, "text": "Even though it's dead. Even though it's dead.", "words": []},
        {"start_sec": 9, "end_sec": 12, "text": "Like my attention decreased. (laughs) Yes.", "words": []},
    ]
    texts = [s["text"] for s in clean_segments(segs)]
    assert texts == ["Okay.", "In this study we were able", "to see and by", "Even though it's dead.",
                     "Like my attention decreased. Yes."]


def test_speaker_change_stays_a_flag_on_the_word():
    segs = [{"start_sec": 0, "end_sec": 3, "text": "Yes. Fine.", "words": [
        {"start": 0.0, "end": 0.4, "text": "Yes.", "speaker_change": True},
        {"start": 0.5, "end": 0.9, "text": "Fine."}]}]
    out = clean_segments(segs)[0]
    assert out["words"][0].get("speaker_change") is True and out["text"] == "Yes. Fine."


def test_the_clip_tail_stops_before_the_next_line_starts():
    line = {"start_sec": 9.0, "end_sec": 10.0, "text": "Done.", "words": []}
    assert tail_end(10.0, [line]) == pytest.approx(10.35)  # nothing after it: the normal tail
    assert tail_end(10.0, [line, {"start_sec": 10.2, "end_sec": 12.0, "text": "Next", "words": []}]) == pytest.approx(10.15)
    assert tail_end(10.0, [line, {"start_sec": 10.0, "end_sec": 12.0, "text": "Next", "words": []}]) == pytest.approx(10.0)


def test_lines_without_word_timings_still_count_for_text_and_sentence_ends():
    segs = [
        {"start_sec": 0.0, "end_sec": 2.0, "text": "Timed line here.", "words": [
            {"start": 0.0, "end": 0.5, "text": "Timed"}, {"start": 0.5, "end": 1.0, "text": "line"},
            {"start": 1.0, "end": 1.8, "text": "here."}]},
        {"start_sec": 2.0, "end_sec": 4.0, "text": "Untimed line ends here.", "words": []},
    ]
    assert "ends" in words_text(segs, 2.0, 4.0)
    assert 4.0 in [round(p, 3) for p in boundaries(segs)["strong_end"]]
