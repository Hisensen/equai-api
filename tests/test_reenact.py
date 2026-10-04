from __future__ import annotations

from pathlib import Path

import pytest

from reenact import director, render
from reenact.align import align_lines, char_error_rate
from reenact.script import cue_ranges, cue_text, parse_line, strip_markup


def test_script_markup_parses_moments_selections_and_dual_text():
    line = parse_line("B", "人家@{brand}<EQUAI|E Q AI>@{/brand}一直都有||@{cheap!}低价渠道。")
    assert line.display == "人家EQUAI一直都有低价渠道。"
    assert line.speech == "人家E Q AI一直都有低价渠道。"
    assert line.selections["brand"] == [2, 3]
    assert line.moments["cheap"] == 7
    assert 7 in line.cue_breaks
    assert strip_markup("<EQUAI|E Q AI>好", spoken=False) == "EQUAI好"


def test_cues_never_split_inside_latin_words():
    line = parse_line("A", "你是真不知道啊，人家全能AI一直都有低价Grok一点五渠道，都稳定六个月了。")
    cues = [cue_text(line, a, b) for a, b in cue_ranges(line)]
    assert all("Gro" not in c or "Grok" in c for c in cues)
    assert "".join(cues).replace("，", "") .startswith("你是真不知道啊")


def test_alignment_places_moments_on_spoken_words():
    line = parse_line("A", "花几元做一个@{price!}十五秒视频")
    chars = [(ch, i * 0.2, i * 0.2 + 0.2) for i, ch in enumerate("花几元做一个十五秒视频")]
    times = align_lines([line], chars, 3.0)
    assert times[0][line.moments["price"]][0] == pytest.approx(1.2)


def test_alignment_interpolates_words_the_recogniser_heard_differently():
    line = parse_line("A", "用的是<1.5|一点五>版本")
    chars = [(ch, i * 0.2, i * 0.2 + 0.2) for i, ch in enumerate("用的是一点无版本")]
    times = align_lines([line], chars, 2.0)
    starts = [t[0] for t in times[0]]
    assert starts == sorted(starts)


def test_char_error_rate_ignores_punctuation():
    assert char_error_rate("你好，世界！", "你好世界") == 0
    assert char_error_rate("abc", "") == 1


def _direction():
    return {"cast": [{"id": "A"}, {"id": "B"}],
            "setups": [{"id": "S1", "visible": ["A", "B"]}, {"id": "S2", "visible": ["B"]}]}


def test_script_check_rejects_moment_components_on_selections_and_duplicates():
    value = {"segments": [{"lines": [
        {"role": "A", "setup": "S1", "text": "@{brand}EQUAI@{/brand}真好"},
        {"role": "B", "setup": "S2", "text": "@{brand}EQUAI@{/brand}稳定"},
    ]}], "components": [{"type": "flash", "moment": "brand"}]}
    problems = director._check_script(value, _direction())
    assert any("more than once" in p for p in problems)
    assert any("Moment" in p for p in problems)


def test_normalise_splits_overlong_segments_and_drops_dangling_components():
    long = "这是一句很长很长的台词，用来测试自动拆分。" * 3
    value = {"segments": [{"id": "x", "lines": [
        {"role": "A", "setup": "S1", "text": long},
        {"role": "B", "setup": "S2", "text": long + "@{hit!}好"},
    ]}], "components": [{"type": "sfx", "moment": "hit"}, {"type": "flash", "moment": "missing"}],
        "captions": {}}
    script = director.normalise_script(value, _direction(), force=True)
    assert len(script["segments"]) == 2
    assert all(5 <= seg["request_duration"] <= 15 for seg in script["segments"])
    assert [c["type"] for c in script["components"]] == ["sfx"]


def test_timeline_offsets_takes_and_resolves_events():
    lines1 = [parse_line("A", "第一句@{a!}话")]
    lines2 = [parse_line("B", "@{s}第二句@{/s}")]
    t1 = [[(0.5, 0.7), (0.7, 0.9), (0.9, 1.1), (1.1, 1.3)]]
    t2 = [[(1.0, 1.2), (1.2, 1.4), (1.4, 1.6)]]
    takes = [render.TakeClip(Path("a.mp4"), 0.0, 2.0), render.TakeClip(Path("b.mp4"), 0.5, 2.5)]
    tl = render.build_timeline(takes, [lines1, lines2], [t1, t2])
    assert tl.duration == pytest.approx(4.0)
    assert tl.moments["a"] == pytest.approx(1.1)
    assert tl.selections["s"] == (pytest.approx(2.5), pytest.approx(3.1))
    assert [c.role for c in tl.cues] == ["A", "B"]


def test_plan_trims_keeps_short_heads_and_trims_long_silence():
    assert render.plan_trims(10, 0.4, 7.0) == (0.0, 7.6)
    assert render.plan_trims(10, 2.0, 9.8) == (1.65, 10)


def test_image_node_retries_with_corrections_until_review_passes(tmp_path, monkeypatch):
    import asyncio
    from reenact import providers, runner

    r = object.__new__(runner.Runner)
    r.task_id, r.work, r.row = "vid_t", tmp_path, {"id": "vid_t", "merchant_id": "m"}
    r.options, r.payload = {}, {}
    from reenact import llm
    r.ledger = llm.Ledger()
    nodes: dict = {}
    r.node = lambda key: nodes.get(key)
    r.save = lambda key, kind, status, output: nodes.__setitem__(key, {"status": status, **output})
    r.persist_options = lambda: None
    submitted = []

    async def submit(text, ratio, refs, idem):
        submitted.append(idem)
        return f"remote-{len(submitted)}"

    async def wait(remote):
        return b"png-" + remote.encode(), {"cost": 1}

    async def put(row, path, name, content_type):
        return {"key": name}

    async def fetch(stored, target):
        return target

    monkeypatch.setattr(providers, "image_submit", submit)
    monkeypatch.setattr(providers, "image_wait", wait)
    monkeypatch.setattr(providers, "put", put)
    monkeypatch.setattr(providers, "fetch", fetch)
    scores = {"setup-S1-1.png": 6, "setup-S1-2.png": 9}

    async def judge(path):
        score = scores[path.name]
        return {"pass": score >= 7, "score": score, "issues": []}

    asyncio.run(r.image_node("setup-S1", "prompt", [], "16:9", judge, asyncio.Semaphore(2)))
    assert len(submitted) == 2  # the first picture fails review, the corrected second one passes
    assert nodes["setup-S1"]["stored"] == {"key": "setup-S1-2.png"}
    assert [c["kind"] for c in r.ledger.calls] == ["image2", "image2"]  # every picture is on the account


def test_user_slots_grant_candidates_atomically_and_cap_concurrency():
    import asyncio
    from reenact.runner import Slots

    async def scenario():
        slots, peak, running = Slots(10), 0, 0

        async def segment():
            nonlocal peak, running
            n = await slots.acquire(2)
            running += n
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= n
            await slots.release(n)

        await asyncio.gather(*(segment() for _ in range(8)))
        return peak

    assert asyncio.run(scenario()) == 10


def test_platform_marks_never_become_graphics():
    analysis = {"platform_overlays": [{"description": "TikTok UI"}],
                "graphics": [{"type": "other", "description": "Account ID 'bingbing66668888' and 'Q 170的冰冰教练'"}]}
    comps = [{"type": "sticker", "text": "抖音号: bingbing66668888"},
             {"type": "headline", "lines": [{"text": "当女生知道"}, {"text": "搜索 哑铃跟练"}]},
             {"type": "logo_card", "text": "EQUAI"}]
    kept = director.drop_platform_marks(comps, analysis)
    assert [c["type"] for c in kept] == ["headline", "logo_card"]
    assert kept[0]["lines"] == [{"text": "当女生知道"}]


def test_product_only_requests_keep_people_and_never_lose_source():
    analysis = {"cast": [{"id": "A"}], "setups": [{"id": "S1"}], "user_pictures": [{"index": 1}], "shots": []}
    value = {"cast": [{"id": "A", "source": "A", "identity": "keep", "person": "the same host", "voice": "design"}],
             "setups": [{"id": "S1", "source": "S1", "visible": ["A"], "shot": "selfie"}], "keep_place": True,
             "products": [{"source": "P1", "label": "水杯", "identity": "upload:1", "description": "clear bottle"},
                          {"label": "杯套", "identity": "upload:5"}]}
    d = director.validate_treatment(value, analysis)
    assert d["cast"][0]["identity"] == "keep" and d["cast"][0]["voice"] == "source:A"
    assert d["keep_place"] is True
    assert [p["identity"] for p in d["products"]] == ["upload:1", "generate"]
    # "keep" without a reference character to keep falls back to a generated one.
    value["cast"][0]["source"] = None
    assert director.validate_treatment(value, analysis)["cast"][0]["identity"] == "generate"


def test_remake_only_uses_graphics_the_reference_has_or_the_user_asks_for():
    analysis = {"graphics": [], "sound": {"sfx": "cap click"}, "style": {"editing": "hard cuts"}}
    script = {"captions": {"position": "bottom"}, "components": [
        {"type": "sticker", "text": "透明水瓶"}, {"type": "reveal_strip", "items": []}, {"type": "sfx", "kind": "pop"}]}
    import copy
    kept = director.keep_reference_graphics(copy.deepcopy(script), analysis, "视频中的水杯换成图中的水杯")
    assert kept["components"] == [] and kept["captions"]["enabled"] is False
    analysis["graphics"] = [{"type": "caption"}, {"type": "sticker"}]
    kept = director.keep_reference_graphics(copy.deepcopy(script), analysis, "加个品牌结尾卡")
    assert [c["type"] for c in kept["components"]] == ["sticker"]
    assert kept["captions"].get("enabled", True) is not False


def test_only_requests_that_change_speech_rewrite_the_soundtrack():
    analysis = {"lines": [{"text": "你懂个蛋，人家都用一元一条的Grok一点五"}]}
    assert director.needs_new_words("同时这个grok换成equai", analysis) is True
    assert director.needs_new_words("台词改成更搞笑的", analysis) is True
    assert director.needs_new_words("视频中的杯子换成图片的，然后人物换成一男一女", analysis) is False
    assert director.needs_new_words("给我换一个人物以及场景，其余保持一样", analysis) is False
    assert director.needs_new_words("把人物换成图1", {"lines": []}) is False


def test_script_check_rejects_a_rewrite_much_longer_than_the_reference():
    value = {"segments": [{"lines": [{"role": "A", "setup": "S1", "text": "这是一句非常非常长的新台词" * 4}]}], "components": []}
    d = {"cast": [{"id": "A"}], "setups": [{"id": "S1", "visible": ["A"]}]}
    assert any("much longer" in p for p in director._check_script(value, d, 6, set(), source_units=10, limit=1.15))


def test_trims_never_keep_a_sliver_of_another_shot():
    # The model cut to a new shot 0.3 s after the last word: the Take ends at that cut.
    assert render.plan_trims(8.0, 0.4, 6.0, [3.1, 6.3]) == (0.0, 6.3)
    # A cut just before the first word starts the Take there.
    assert render.plan_trims(8.0, 1.5, 6.0, [1.3]) == (1.3, 6.6)


def test_performance_prompt_follows_the_h3_full_reference_format():
    from reenact import kits
    cast = {"A": {"label": "悟空", "person": "a monkey king in jade silk."},
            "B": {"label": "嫂子", "person": "a woman in a red suit."}}
    setups = {"S1": {"visible": ["A", "B"], "shot": "Wide two-shot."}, "S2": {"visible": ["B"], "shot": "Close on B."},
              "S3": {"visible": [], "shot": "The cup on a café table."}}
    segment = {"request_duration": 8, "lines": [
        {"role": "B", "setup": "S2", "text": "打的就是你这个泼猴！", "action": "She points."},
        {"role": "A", "setup": "S1", "text": "<Grok1.5|Grok 一点五>很便宜", "action": ""}]}
    prompt = kits.performance_prompt(segment=segment, cast=cast, setups=setups, pictures=["S2", "S1", "S3"],
                                     audio_for={"B": 1}, speaker_ids={"A": 1, "B": 2}, setting="An office.",
                                     fmt="skit", ratio="9:16", estimates=[2.0, 2.0])
    assert "<d>[Chinese] 打的就是你这个泼猴！</d>" in prompt
    assert "<d>[Chinese] Grok 一点五很便宜。</d>" in prompt          # spoken form, closed with punctuation
    assert "(S1) says using the voice timbre referenced from <Audio 1>" in prompt  # first speaker here is S1
    assert "[Shot 1] The shot's keyframe corresponds to <Picture 1>." in prompt
    assert "[Shot 2] At 00:02.250, the shot cuts to the keyframe of <Picture 2>." in prompt
    assert "<Picture 1> ([Shot 1] keyframe): fully_preserved" in prompt
    assert "<Picture 3> is the keyframe of any cutaway shot, showing no people" in prompt
    assert ": reference -" not in prompt.split("<Audio 1>: reference")[0]      # pictures never use the audio marker


def test_image_prompts_stay_within_the_services_5000_character_limit():
    from reenact import kits
    long = "A very detailed description of a character with many accessories and a long outfit. " * 30
    cast = {c: {"label": c, "person": long} for c in "ABCD"}
    setup = {"visible": list("ABCD"), "shot": long}
    prompt = kits.setup_image_prompt(capture="cinematic", setup=setup, cast=cast, setting=long,
                                     inputs=[("main", ""), ("layout", "")], style="漫画风")
    assert len(prompt) <= 4800
    fixed = kits.fit_image_prompt(prompt, ["Only A may appear; remove anyone else."] * 50)
    assert len(fixed) <= 4800 and "MUST FIX" in fixed or len(prompt) > 4700
    assert "not photographic" in prompt  # an illustrated style leads instead of film language


def test_voice_pitch_separates_low_and_high_voices_and_measures_loudness(tmp_path):
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    from reenact.media import voice_features
    source = tmp_path / "voices.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=110:duration=1", "-f", "lavfi",
                    "-i", "sine=frequency=240:duration=1", "-filter_complex",
                    "[1]volume=0.1[q];[0][q]concat=n=2:v=0:a=1", str(source)], check=True, timeout=30)
    low, high = voice_features(source, 0.0, 1.0), voice_features(source, 1.0, 2.0)
    assert 100 < low["pitch"] < 120 and 225 < high["pitch"] < 255
    assert high["level"] < low["level"] - 15  # the quiet background voice


def test_a_reference_without_people_is_valid_and_its_speech_is_off_screen():
    from reenact import analysis
    sentences = [{"start": 0.5, "end": 2.0, "text": "千年之后的你会在哪里", "words": []}]
    a = analysis.validate({"cast": [], "setups": [{"id": "S1", "framing": "galaxy vista", "visible": []}],
                           "shots": [{"start": 0, "end": 4, "setup": "S1"}]}, 4.0, sentences)
    assert a["cast"] == [] and a["lines"][0]["speaker"] == analysis.VO
    direction = director.validate_treatment(
        {"cast": [], "setting": "a nebula", "audio_strategy": "reuse",
         "setups": [{"id": "S1", "visible": [], "shot": "slow push through a nebula"}]}, a)
    assert direction["cast"] == [] and direction["setups"][0]["visible"] == []
    a["lines"][0]["offscreen"] = True
    segments = director.plan_reuse_segments(a, direction)
    assert segments[0]["lines"][0]["role"] == analysis.VO and segments[0]["lines"][0]["offscreen"]


def test_motion_prompt_keeps_off_screen_and_background_voices_off_everyone_s_lips():
    from reenact import kits
    cast = {"A": {"label": "余念", "person": "a young woman"}, "B": {"label": "苏鹤白", "person": "a tall man"}}
    setups = {"S1": {"visible": ["A", "B"], "shot": "two-shot"}, "S2": {"visible": ["A"], "shot": "close-up of A"}}
    segment = {"lines": [{"role": "B", "text": "余念，你攻略错人了。"},
                         {"role": "VO", "text": "好的，主神。", "offscreen": True}],
               "shot_plan": [{"start": 0, "setup": "S1", "action": "B leans in."}]}
    prompt = kits.motion_performance_prompt(segment=segment, cast=cast, setups=setups, pictures=["S1", "S2"],
                                            speaker_ids={}, setting="an office", ratio="16:9", duration=8,
                                            reuse_audio=True, lyrics=[{"text": "电视里的新闻", "kind": "background"}])
    assert "<Subject 2> (S1) says <d>[Chinese] 余念，你攻略错人了。</d>" in prompt
    assert "An off-screen voice (S2) says <d>[Chinese] 好的，主神。</d> in <Audio 1>; nobody on screen" in prompt
    assert "come from the background (off screen)" in prompt
    empty = kits.motion_performance_prompt(segment={"lines": [], "shot_plan": []}, cast={},
                                           setups={"S1": {"visible": [], "shot": "galaxy"}}, pictures=["S1"],
                                           speaker_ids={}, setting="deep space", ratio="16:9", duration=10,
                                           reuse_audio=True, lyrics=[{"text": "千年之后", "kind": "lyrics"}])
    assert "with no people" in empty and "No person or character appears" in empty and "do not sing" not in empty


def test_reference_captions_allow_rebuilt_lyric_headlines():
    allowed = director.allowed_graphics({"graphics": [{"type": "caption"}]}, "唯美一些")
    assert {"captions", "headline"} <= allowed
    assert "headline" in director.allowed_graphics({"graphics": []}, "同时有字幕")


def test_text_track_merges_frames_and_ignores_one_frame_ocr_slips():
    from reenact.analysis import merge_text_frames
    frames = [{"t": 0.0, "lines": []},
              {"t": 0.5, "lines": ["如果转换了时空身份和姓名", "If the time-space identity"]},
              {"t": 1.0, "lines": ["如果转换了时空身份和姓名", "If the time-space identity and name are converted"]},
              {"t": 1.5, "lines": ["如果转换了时空身分和姓名", "If the time-space identity and name are converted"]},
              {"t": 2.0, "lines": ["但愿认得你眼睛", "I wish I knew your eyes"]},
              {"t": 2.5, "lines": ["但愿认得你眼睛", "I wish I knew your eyes"]}]
    track = merge_text_frames(frames, 0.5)
    assert [(x["start"], x["end"]) for x in track] == [(0.5, 2.0), (2.0, 3.0)]
    assert track[0]["lines"][1] == "If the time-space identity and name are converted"


def test_a_clearly_male_voice_is_never_given_to_the_only_woman():
    from reenact.analysis import VO, voice_matches
    cast = [{"id": "A", "gender": "female"}, {"id": "B", "gender": "male"}]
    assert voice_matches("A", 75.5, cast) == "B"
    assert voice_matches("B", 290.0, cast) == "A"
    assert voice_matches("A", 170.0, cast) == "A"          # ambiguous band: the judges decide
    assert voice_matches("A", 80.0, [{"id": "A", "gender": "female"}]) == VO
    assert voice_matches("A", 80.0, [{"id": "A", "gender": "unknown"}]) == "A"


def test_the_reference_text_track_always_comes_back_unless_the_user_refuses_text():
    analysis = {"text_track": [{"start": 0.5, "end": 6.0, "position": "bottom",
                                "lines": ["如果转换了时空身份和姓名", "If the time-space identity and name are converted"]},
                               {"start": 6.0, "end": 10.5, "position": "bottom", "lines": ["但愿认得你眼睛"]}]}
    edited = [{"type": "headline", "source": [6.0, 10.4], "lines": [{"text": "但愿认得你的眼睛"}]}]
    out = director.rebuild_text_track(edited, analysis, "唯美一些，同时有字幕")
    assert len(out) == 2 and out[0] is edited[0]                     # the editor's version of line 2 wins
    assert out[1]["source"] == [0.5, 6.0] and out[1]["lines"][1]["size"] == "s"
    assert director.rebuild_text_track([], analysis, "不要字幕") == []


def _verdict_with(claims, speech=()):
    return {"pass": False, "score": 4.0, "issues": list(speech) + list(claims), "visual": {"issues": list(claims)}}


def test_take_claims_are_confirmed_before_another_take_is_paid_for(monkeypatch):
    import asyncio
    from reenact import llm, qa
    answers = {}

    async def fake_vision(content, ledger, kind="", max_tokens=0):
        return answers["value"]
    monkeypatch.setattr(llm, "vision", fake_vision)
    run = lambda v: asyncio.run(qa.confirm_take("u", v, cast_lines="", people="", ledger=llm.Ledger()))

    answers["value"] = {"claims": [{"i": 0, "confirmed": False}]}
    out = run(_verdict_with(["the frames look static"]))
    assert out["pass"] and out["score"] >= qa.PASS_SCORE and out["issues"] == []

    answers["value"] = {"claims": [{"i": 0, "confirmed": True, "cause": "setup"}]}
    out = run(_verdict_with(["the dress differs from setup S1"]))
    assert not out["pass"] and out["input_problem"]

    answers["value"] = {"claims": [{"i": 0, "confirmed": False}]}
    out = run(_verdict_with(["static"], speech=["The spoken words differ from the script"]))
    assert not out["pass"] and out["issues"] == ["The spoken words differ from the script"]


def test_setup_consistency_only_names_known_setups(monkeypatch, tmp_path):
    import asyncio
    from PIL import Image
    from reenact import llm, qa
    path = tmp_path / "s.png"
    Image.new("RGB", (64, 36)).save(path)

    async def fake_vision(content, ledger, kind="", max_tokens=0):
        return {"outliers": [{"setup": "S1", "character": "余念", "problem": "black jacket instead of crimson dress"},
                             {"setup": "S9", "character": "x", "problem": "unknown"}]}
    monkeypatch.setattr(llm, "vision", fake_vision)
    out = asyncio.run(qa.review_setup_consistency([("S1", path, ["余念"]), ("S2", path, ["余念"])], "", llm.Ledger()))
    assert [o["setup"] for o in out] == ["S1"]


def test_chroma_tells_a_grey_copy_from_a_coloured_frame_even_in_the_dark(tmp_path):
    from PIL import Image
    from reenact.media import saturation
    Image.new("RGB", (64, 36), (40, 90, 200)).save(tmp_path / "blue.png")
    Image.new("RGB", (64, 36), (120, 120, 124)).save(tmp_path / "grey.png")
    Image.new("RGB", (64, 36), (4, 3, 5)).save(tmp_path / "night.png")   # near-black noise is not colour
    assert saturation(tmp_path / "blue.png") > 100
    assert saturation(tmp_path / "grey.png") < 5 and saturation(tmp_path / "night.png") < 5


def test_signed_cos_links_go_through_the_download_relay_only_while_it_is_up(monkeypatch):
    import object_storage
    monkeypatch.setenv("COS_BUCKET", "b-1")
    monkeypatch.setenv("COS_REGION", "ap-guangzhou")
    monkeypatch.setenv("COS_DOWNLOAD_RELAY", "https://129.204.50.9/cos")
    url = "https://b-1.cos.ap-guangzhou.myqcloud.com/outputs/x/a.mp4?q-sign-algorithm=sha1&q-signature=abc"
    monkeypatch.setattr(object_storage, "_prober_started", True)
    object_storage._relay_state.update(checked=9e18, up=True)
    assert object_storage.via_relay(url) == "https://129.204.50.9/cos/outputs/x/a.mp4?q-sign-algorithm=sha1&q-signature=abc"
    object_storage._relay_state.update(checked=9e18, up=False)
    assert object_storage.via_relay(url) == url
    monkeypatch.delenv("COS_DOWNLOAD_RELAY")
    object_storage._relay_state.update(checked=9e18, up=True)
    assert object_storage.via_relay(url) == url
    object_storage._relay_state.update(checked=0.0, up=False)


def test_the_relay_counts_as_down_a_day_before_its_certificate_expires(monkeypatch):
    import ssl
    import object_storage

    class FakeTLS:
        def __init__(self, hours): self.hours = hours
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def wrap_socket(self, raw, server_hostname=None): return self
        def getpeercert(self):
            import time
            return {"notAfter": time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(time.time() + self.hours * 3600))}

    class Raw:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: Raw())
    monkeypatch.setenv("COS_BUCKET", "b-1")
    monkeypatch.setenv("COS_REGION", "ap-guangzhou")

    class Signer:
        def get_presigned_url(self, **k):
            return "https://b-1.cos.ap-guangzhou.myqcloud.com/relay-canary/a%20b%2Bc?sig=1"

    class Reply:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n): return b"relay-canary"

    fetched = []
    monkeypatch.setattr(object_storage, "client", lambda: Signer())
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: fetched.append(url) or Reply())
    for hours, expected in ((72, True), (20, False)):
        monkeypatch.setattr(ssl, "create_default_context", lambda h=hours: FakeTLS(h))
        up, left = object_storage._probe_relay("https://129.204.50.9/cos", __import__("time").time())
        assert up is expected and abs(left - hours) < 1
    assert fetched == ["https://129.204.50.9/cos/relay-canary/a%20b%2Bc?sig=1"]  # the canary goes through the relay verbatim

    def broken(url, timeout=0):
        raise OSError("403 SignatureDoesNotMatch")
    monkeypatch.setattr("urllib.request.urlopen", broken)
    monkeypatch.setattr(ssl, "create_default_context", lambda: FakeTLS(72))
    assert object_storage._probe_relay("https://129.204.50.9/cos", __import__("time").time())[0] is False


def test_pipeline_code_has_no_undefined_or_shadowed_names():
    """A name used before its assignment in the same function (e.g. a later `expected = ...` shadowing an outer
    `expected`) crashes every take at runtime; pyflakes finds it statically."""
    pyflakes = pytest.importorskip("pyflakes.api")
    from pyflakes import reporter as pyflakes_reporter
    import io
    root = Path(__file__).resolve().parents[1]
    files = sorted((root / "reenact").glob("*.py")) + [root / "object_storage.py", root / "admin_api.py"]
    out = io.StringIO()
    pyflakes.checkRecursive([str(f) for f in files], pyflakes_reporter.Reporter(out, out))
    fatal = [l for l in out.getvalue().splitlines()
             if "referenced before assignment" in l or "undefined name" in l]
    assert not fatal, "\n".join(fatal)


def test_a_worker_that_cannot_fetch_through_the_relay_pauses_it(monkeypatch):
    import time
    import object_storage
    monkeypatch.setenv("COS_DOWNLOAD_RELAY", "https://129.204.50.9/cos")
    monkeypatch.setattr(object_storage, "_prober_started", True)
    object_storage._relay_state.update(checked=time.time(), up=True)
    assert not object_storage.relay_failed("stage=render: CUDA out of memory")
    assert object_storage._relay_state["up"]
    assert object_storage.relay_failed("stage=input_prepare: HTTPStatusError: Client error '403 Forbidden' for url "
                                       "'https://129.204.50.9/cos/inputs/x/edited%20%281%29.png?q-sign=1'")
    assert object_storage._relay_base() == ""                      # COS links from now on
    assert object_storage._relay_state["checked"] > time.time() + 3000   # and no re-probe for an hour
    object_storage._relay_state.update(checked=0.0, up=False)


def test_pages_may_load_media_from_the_download_relay(monkeypatch):
    import main
    monkeypatch.setenv("COS_BUCKET", "b-1")
    monkeypatch.setenv("COS_REGION", "ap-guangzhou")
    monkeypatch.setenv("H3_UPLOAD_RELAY_BASE_URL", "https://129.204.50.9")
    monkeypatch.setenv("COS_DOWNLOAD_RELAY", "https://129.204.50.9/cos")
    assert main._storage_csp_sources() == ["https://b-1.cos.ap-guangzhou.myqcloud.com", "https://129.204.50.9"]
    monkeypatch.setenv("H3_UPLOAD_RELAY_BASE_URL", "https://equai.cn")
    assert "https://129.204.50.9" in main._storage_csp_sources() and "https://equai.cn" in main._storage_csp_sources()


def test_uploads_prefer_the_ip_relay_fall_back_and_return_to_it_when_repaired(monkeypatch):
    import object_storage
    health = {"https://129.204.50.9": False, "https://equai.cn": True}
    probes = []
    monkeypatch.setattr(object_storage, "_probe_upload_relay", lambda base, now: probes.append(base) or health[base])
    monkeypatch.setattr(object_storage, "_upload_state", {})
    monkeypatch.setattr(object_storage, "_prober_started", True)   # no background thread in tests
    monkeypatch.delenv("H3_UPLOAD_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("H3_UPLOAD_RELAY_FALLBACK_URL", raising=False)
    assert object_storage.upload_relay() is None                        # nothing configured: plain COS grants
    monkeypatch.setenv("H3_UPLOAD_RELAY_BASE_URL", "https://129.204.50.9")
    monkeypatch.setenv("H3_UPLOAD_RELAY_FALLBACK_URL", "https://equai.cn")
    assert object_storage.upload_relay() == "https://equai.cn"         # IP down: the fallback relay
    health["https://equai.cn"] = False
    object_storage._upload_state.clear()
    assert object_storage.upload_relay() == ""                          # both down: uploads go through the API
    health["https://129.204.50.9"] = True
    assert object_storage.upload_relay() == ""                          # requests only read the cached verdict...
    for base in list(object_storage._upload_state):
        object_storage._upload_state[base] = (0.0, False)
    object_storage._refresh_upload_relays()                             # ...the background check re-probes...
    assert object_storage.upload_relay() == "https://129.204.50.9"     # ...and the repaired IP is used again
