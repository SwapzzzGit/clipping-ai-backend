import json

import pytest

from clipper import candidates as cand
from clipper.util import extract_json
from tests.conftest import make_segments


class FakeLLM:
    """Returns scripted replies in order; records prompts."""

    name = "fake:test"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user, fallback=None):
        self.prompts.append((system, user))
        reply = self.replies.pop(0)
        return reply(user) if callable(reply) else reply


def scores_for(ids, **override):
    base = {c: 5 for c in cand.CRITERIA}
    return {"scores": [{"id": i, **base, **override, "reason": "r"} for i in ids]}


def test_build_windows_overlap_and_alignment(long_segments):
    windows = cand.build_windows(long_segments, length=75, stride=30)
    assert len(windows) > 5
    starts = {s["start_sec"] for s in long_segments}
    for w in windows[:-1]:
        assert w["start_sec"] in starts  # aligned to caption segments
        assert 60 <= w["end_sec"] - w["start_sec"] <= 80
    for prev, w in zip(windows, windows[1:]):
        assert w["end_sec"] > prev["end_sec"]  # no window fully inside the previous one
    assert windows[-1]["end_sec"] == long_segments[-1]["end_sec"]
    assert windows[1]["start_sec"] - windows[0]["start_sec"] == pytest.approx(30, abs=3.5)


def test_final_score_weights_and_heatmap_bonus():
    s = dict.fromkeys(cand.CRITERIA, 6.0)
    weights = {c: 1 for c in cand.CRITERIA}
    assert cand.final_score(s, weights, heat=0.0, bonus=1.0, threshold=0.5) == 6.0
    assert cand.final_score(s, weights, heat=0.4, bonus=1.0, threshold=0.5) == 6.0  # below threshold
    assert cand.final_score(s, weights, heat=0.9, bonus=1.0, threshold=0.5) == 6.9
    heatmap = [{"start_sec": 0, "end_sec": 10, "value": 0.2}, {"start_sec": 10, "end_sec": 20, "value": 0.9}]
    assert cand.heat_for(5, 15, heatmap) == 0.9
    assert cand.heat_for(30, 40, heatmap) == 0.0


def test_validate_pass1_clamps_and_rejects_missing():
    ok = cand.validate_pass1({"scores": [{"id": 1, **dict.fromkeys(cand.CRITERIA, 12), "reason": "x"},
                                         {"id": 2, **dict.fromkeys(cand.CRITERIA, "7")}]}, [1, 2])
    assert ok[1]["hook_strength"] == 10.0 and ok[2]["emotion"] == 7.0
    with pytest.raises(ValueError, match="missing"):
        cand.validate_pass1({"scores": [{"id": 1, **dict.fromkeys(cand.CRITERIA, 5)}]}, [1, 2])
    with pytest.raises(ValueError, match="must be a number"):
        cand.validate_pass1({"scores": [{"id": 1, **dict.fromkeys(cand.CRITERIA, "high")}]}, [1])


def test_ask_json_retries_once_then_succeeds():
    llm = FakeLLM(["not json at all", "```json\n" + json.dumps(scores_for([0])) + "\n```"])
    out = cand.ask_json(llm, "sys", "user", lambda r: cand.validate_pass1(r, [0]))
    assert out[0]["hook_strength"] == 5
    assert "previous reply was invalid" in llm.prompts[1][1]


def test_ask_json_gives_up_after_two_failures():
    llm = FakeLLM(["nope", "{}"])
    with pytest.raises(ValueError):
        cand.ask_json(llm, "sys", "user", lambda r: cand.validate_pass1(r, [0]))


def test_extract_json_tolerates_prose():
    assert extract_json('Sure! Here you go: {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_snapping_prefers_sentence_boundaries(cfg):
    segs = make_segments(["one two three four five.", "six seven eight nine ten.", "eleven twelve."], gap=0)
    b = cand.boundaries(segs)
    assert 2.0 in b["strong_start"] and 2.0 in b["strong_end"]
    assert cand.snap(2.7, b, "start", tol=4) == 2.0  # mid-sentence start moves to sentence start
    cfg["clips"].update(min_sec=1, max_sec=5)
    assert cand.fit_clip(0.3, 3.1, b, cfg) == (0.0, 4.0)


def test_speaker_change_is_a_boundary():
    segs = make_segments(["so what do you think", "well I think it is"], gap=0)
    segs[1]["words"][0]["speaker_change"] = True
    b = cand.boundaries(segs)
    assert segs[1]["words"][0]["start"] in b["strong_start"]


def test_fit_clip_enforces_max_length(cfg, long_segments):
    b = cand.boundaries(long_segments)
    start, end = cand.fit_clip(10, 200, b, cfg)
    assert cfg["clips"]["min_sec"] <= end - start <= cfg["clips"]["max_sec"]
    assert end in b["strong_end"]


def test_align_hook_finds_exact_start():
    segs = make_segments(["pretty quickly, huh? So the only way out is to stop that."])
    at = cand.align_hook("So the only way out is to stop that.", 0.0, segs)
    assert at == segs[0]["words"][3]["start"]
    assert cand.align_hook("Something never said here", 0.0, segs) is None


def _scored_windows(segments, cfg):
    windows = cand.build_windows(segments, 75, 30)
    out = []
    for w in windows:
        s = {**dict.fromkeys(cand.CRITERIA, 5.0 + w["id"] % 4), "reason": "r", "violates_rules": False}
        out.append({**w, "scores": s, "heat": 0.0, "final_score": 5.0 + w["id"] % 4})
    return out


def test_select_clips_validates_llm_picks(cfg, long_segments):
    cfg["num_clips"] = 3
    scored = _scored_windows(long_segments, cfg)
    regions = cand.merge_regions(sorted(scored, key=lambda w: -w["final_score"])[:6])
    r0 = regions[0]
    reply = {"clips": [
        # valid pick, hook given mid-line
        {"region_id": r0["id"], "start_sec": r0["start_sec"], "end_sec": r0["start_sec"] + 40,
         "title": "Good one", "hook_line": long_segments[[s["start_sec"] for s in long_segments].index(r0["start_sec"]) + 1]["text"],
         "reason": "great"},
        # overlaps the first pick: must be dropped
        {"region_id": r0["id"], "start_sec": r0["start_sec"] + 5, "end_sec": r0["start_sec"] + 35,
         "title": "Dup", "hook_line": "", "reason": ""},
        # far too long: gets trimmed to max_sec
        {"region_id": regions[-1]["id"], "start_sec": regions[-1]["start_sec"], "end_sec": regions[-1]["start_sec"] + 500,
         "title": "Long", "hook_line": "", "reason": ""},
    ]}
    clips = cand.select_clips(regions, long_segments, scored, FakeLLM([json.dumps(reply)]), cfg, rules="")
    assert len(clips) == 3  # 2 valid LLM picks + 1 fallback top-up
    spans = sorted((c["start_sec"], c["end_sec"]) for c in clips)
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a1 <= b0  # no overlaps
    for c in clips:
        assert cfg["clips"]["min_sec"] <= c["end_sec"] - c["start_sec"] <= cfg["clips"]["max_sec"]
        assert c["title"] and c["hook_line"] and c["text"]
    good = next(c for c in clips if c["title"] == "Good one")
    assert good["text"].startswith(good["hook_line"])  # clip starts exactly on the hook
    assert sorted(c["selected_by"] for c in clips) == ["fallback", "llm", "llm"]
    assert [c["id"] for c in clips] == [1, 2, 3]


def test_select_clips_survives_garbage_from_llm(cfg, long_segments):
    cfg["num_clips"] = 2
    scored = _scored_windows(long_segments, cfg)
    regions = cand.merge_regions(scored[:4])
    clips = cand.select_clips(regions, long_segments, scored, FakeLLM(["garbage", "still garbage"]), cfg, "")
    assert clips and all(c["selected_by"] == "fallback" for c in clips)


def test_campaign_rules_are_injected_as_hard_constraints(cfg, long_segments, tmp_path):
    cfg["windows"]["batch_size"] = 100
    windows = cand.build_windows(long_segments, 75, 30)

    def reply(user):
        return json.dumps(scores_for([w["id"] for w in windows]))

    llm = FakeLLM([reply])
    cand.score_windows(windows, llm, cfg, "DON'T mention politics.", tmp_path / "scores.json")
    system = llm.prompts[0][0]
    assert "HARD CONSTRAINTS" in system and "DON'T mention politics." in system


def test_score_windows_resumes_from_cache(cfg, long_segments, tmp_path):
    cfg["windows"]["batch_size"] = 4
    cfg["llm"]["concurrency"] = 1
    windows = cand.build_windows(long_segments, 75, 30)
    ids = [w["id"] for w in windows]
    first_batch = ids[:4]
    # First run: batch 1 succeeds, all the others fail twice.
    llm = FakeLLM([json.dumps(scores_for(first_batch))] + ["bad"] * 100)
    with pytest.raises(Exception):
        cand.score_windows(windows, llm, cfg, "", tmp_path / "scores.json")
    # Second run only asks about the windows that are still missing.
    asked = []

    def reply(user):
        batch = [i for i in ids if f"### Window {i}\n" in user]
        asked.extend(batch)
        return json.dumps(scores_for(batch))

    scores = cand.score_windows(windows, FakeLLM([reply] * 100), cfg, "", tmp_path / "scores.json")
    assert set(scores) == set(ids)
    assert not set(asked) & set(first_batch)


def test_violating_windows_are_excluded(cfg, long_segments, tmp_path):
    from clipper.llm import LLM  # noqa: F401  (ensures module imports cleanly)

    cfg["windows"]["batch_size"] = 100
    cfg["num_clips"] = 2
    windows = cand.build_windows(long_segments, 75, 30)
    bad = {w["id"] for w in windows if w["start_sec"] < 120}

    def pass1(user):
        return json.dumps({"scores": [
            {"id": w["id"], **dict.fromkeys(cand.CRITERIA, 9 if w["id"] in bad else 4),
             "violates_rules": w["id"] in bad, "reason": ""} for w in windows]})

    llm = FakeLLM([pass1, "garbage", "garbage"])
    transcript = {"segments": long_segments, "heatmap": []}
    clips = cand.find_clips(transcript, tmp_path, llm, cfg, rules="DON'T x")
    assert all(c["start_sec"] >= 90 for c in clips)


# -- review marks -----------------------------------------------------------------------

def test_snap_mark_start_keeps_word_under_playhead():
    segs = make_segments(["a b c d.", "e f g h."])
    assert cand.snap_mark(0.9, "start", segs, 0, 3.3) == 0.8   # inside c -> c.start
    assert cand.snap_mark(1.1, "start", segs, 0, 3.3) == 1.2   # last 0.15 s of c -> next word d


def test_snap_mark_end_keeps_finished_word():
    segs = make_segments(["a b c d.", "e f g h."])
    assert cand.snap_mark(1.0, "end", segs, 0, 3.3) == 1.2     # inside c -> c.end
    assert cand.snap_mark(0.85, "end", segs, 0, 3.3) == 0.8    # first 0.15 s of c -> b.end


def test_snap_mark_clamps_to_clip():
    segs = make_segments(["a b c d.", "e f g h."])
    assert cand.snap_mark(0.0, "start", segs, 0.8, 2.9) == 0.8
    assert cand.snap_mark(9.0, "end", segs, 0.8, 2.9) == 2.9


def test_snap_mark_without_word_timings_uses_segments():
    segs = [{"start_sec": 0, "end_sec": 2, "text": "x", "words": []},
            {"start_sec": 2.5, "end_sec": 4, "text": "y", "words": []}]
    assert cand.snap_mark(2.6, "start", segs, 0, 4) == 2.5


def test_words_text_covers_range():
    segs = make_segments(["a b c d.", "e f g h."])
    assert cand.words_text(segs, 0.8, 2.5) == "c d. e f"
