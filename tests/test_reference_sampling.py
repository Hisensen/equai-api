from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


def _load_server(tmp_path: Path):
    profile = types.SimpleNamespace(
        fl_model="fl.safetensors",
        ref_model="ref.safetensors",
        text_encoder="text.safetensors",
        video_vae="video.safetensors",
        audio_vae="audio.safetensors",
        fl_lora="fl-lora.safetensors",
        ref_lora="ref-lora.safetensors",
        fl_steps=8,
        ref_steps=4,
    )
    profiles = types.ModuleType("h3_profiles")
    profiles.get_profile = lambda _key: profile
    profiles.profile_status = lambda _root: {"profiles": {}}
    previous = sys.modules.get("h3_profiles")
    sys.modules["h3_profiles"] = profiles
    try:
        spec = importlib.util.spec_from_file_location(
            "worker_long_server_reference_test",
            Path(__file__).parents[1] / "worker_long" / "server.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("h3_profiles", None)
        else:
            sys.modules["h3_profiles"] = previous


def test_reference_sampling_uses_six_steps(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("H3_ROOT", str(tmp_path))
    monkeypatch.delenv("H3_REFERENCE_STEPS", raising=False)
    server = _load_server(tmp_path)

    nodes = server.sampling_nodes(
        "40", 123, "h3/reference", "standard", reference=True
    )

    assert nodes["52"]["inputs"]["steps"] == 6
    assert nodes["52"]["inputs"]["scheduler"] == "simple"
    assert nodes["53"]["inputs"]["sampler_name"] == "euler"
    assert nodes["9"]["inputs"]["strength_model"] == 1.0
    assert nodes["103"]["inputs"]["shift_video"] == 12.0
    assert nodes["103"]["inputs"]["shift_audio"] == 3.0


def test_non_reference_sampling_keeps_profile_steps(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("H3_ROOT", str(tmp_path))
    monkeypatch.delenv("H3_REFERENCE_STEPS", raising=False)
    server = _load_server(tmp_path)

    nodes = server.sampling_nodes(
        "20", 123, "h3/text", "standard", reference=False
    )

    assert nodes["52"]["inputs"]["steps"] == 8
    assert nodes["52"]["inputs"]["scheduler"] == "beta"


def test_every_h3_generation_graph_uses_solattn(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("H3_ROOT", str(tmp_path))
    monkeypatch.setenv("H3_SOLATTN", "tau:1.3:0.0:1.0")
    server = _load_server(tmp_path)
    graphs = [
        server.text_workflow("test", 15, "16:9", 0.9, 1),
        server.image_workflow("test", "person.png", None, 15, "16:9", 0.9, 1),
        server.reference_workflow("test", ["person.png"], [], ["voice.mp3"], 15, "16:9", 0.9, 1),
        server.enhance_workflow("test", "source.mp4", 15, "16:9", 0.9, 1),
        server.latent_refine_workflow("test", "video.latent", "audio.latent", 15, "16:9", 0.9, 1),
    ]
    for graph in graphs:
        assert graph["104"]["class_type"] == "SolAttnMiniMax"
        assert graph["104"]["inputs"]["model"] in (["20", 0], ["40", 0])

    hd_graphs = [
        server.text_latent_hd_workflow("test", 15, "16:9", 0.9, 1.0, 1),
        server.image_latent_hd_workflow("test", "person.png", None, 15, "16:9", 0.9, 1.0, 1),
        server.reference_latent_hd_workflow("test", ["person.png"], [], ["voice.mp3"], 15, "16:9", 0.9, 1.0, 1),
    ]
    for graph in hd_graphs:
        assert graph["104"]["class_type"] == "SolAttnMiniMax"
        assert graph["304"]["class_type"] == "SolAttnMiniMax"
