"""Focused coverage for the isolated two-profile SE tuning harness."""

from __future__ import annotations

from copy import deepcopy
import inspect
import math
import os
from pathlib import Path

import pytest

import se_tuning_loop as harness


def test_isolation_filter_complex_arithmetic_keeps_fail_closed_path_detection() -> None:
    graph = (
        "[0:a]atrim=start=0:end=7,asetpts=N/SR/TB,apad=whole_dur=7[a0];"
        "[1:a]atrim=start=0:end=1,asetpts=N/SR/TB[a1];"
        "[a0][a1]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
        "alimiter=limit=0.95:level=false:latency=true,volume=0dB,"
        "apad=whole_dur=7,atrim=start=0:end=7[aout]"
    )

    probe = harness._candidate_argv_path_probe_token("-filter_complex", graph)

    assert "N/SR/TB" not in probe
    assert "/" not in probe
    assert graph.count("asetpts=N/SR/TB") == 2
    assert harness._filtergraph_uses_only_delivery_filters(
        "[dialogue][bgm][se]amix="
        "inputs=3:duration=longest:dropout_transition=0:normalize=0"
    )

    unsafe_graphs = (
        f"{graph};amovie=../secret.wav[rogue]",
        f"{graph};amovie=secret.wav[rogue]",
        "movie=asetpts=N/SR/TB[rogue]",
        "amovie=',asetpts=N/SR/TB[rogue]'",
        f"{graph};drawtext=textfile=secret.txt[rogue]",
    )
    for unsafe_graph in unsafe_graphs:
        unsafe_probe = harness._candidate_argv_path_probe_token(
            "-filter_complex", unsafe_graph
        )
        assert "/" in unsafe_probe
    assert (
        harness._candidate_argv_path_probe_token("-filter_complex_script", graph)
        == graph
    )


@pytest.fixture(scope="module")
def real_evaluation(tmp_path_factory):
    parent = tmp_path_factory.mktemp("se-two-profile")
    return harness.evaluate_two_profiles(parent / "evaluation")


@pytest.fixture(scope="module")
def complete_runs(tmp_path_factory):
    parent = tmp_path_factory.mktemp("se-complete-loop")
    first = harness.run_tuning_loop(parent / "run-a")
    second = harness.run_tuning_loop(parent / "run-b")
    return first, second


def _identity(event: str, digest_character: str, source_time: float) -> dict:
    record = {
        "event_identity": event,
        "category": "impact",
        "authoritative_source_time_sec": source_time,
        "asset_sha256": digest_character * 64,
    }
    record["identity"] = harness._identity_key(record)
    return record


def _cue(event: str, digest_character: str, source_time: float) -> dict:
    identity = _identity(event, digest_character, source_time)
    semantic = source_time - harness.HIGHLIGHT_START_SEC
    return {
        **identity,
        "intensity": 0.72,
        "reason": "fixed",
        "semantic_clip_time_sec": semantic,
        "final_clip_time_sec": semantic,
        "known_scene_clip_time_sec": semantic,
        "signed_snap_shift_sec": 0.0,
        "absolute_snap_shift_sec": 0.0,
        "absolute_expected_scene_error_sec": 0.0,
        "event_source": "transcript",
        "snapped": False,
        "excitement_peak_score": None,
        "cue_window_audibility": {
            "stem_delta_db": 20.0,
            "mixed_delta_db": 18.0,
            "stem_onset_error_sec": 0.0,
            "audible": True,
        },
    }


def _profile(name: str, usage: float, cue_specs: tuple[tuple[str, str, float], ...]):
    cues = tuple(_cue(*spec) for spec in cue_specs)
    expected = tuple(_identity(*spec) for spec in cue_specs)
    actual = tuple(dict(identity) for identity in expected)
    count = len(cues)
    profile = {
        "profile": name,
        "usage_percent": usage,
        "expected_plan": expected,
        "actual_plan": actual,
        "measured_cues": cues,
        "metrics": {
            "decoded_4x_peak_dbfs": -2.0,
            "limiter_margin_db": 1.0,
            "post_mix_attenuation_db": 0.0,
            "audible_cue_count": count,
            "audible_cue_density": count / harness.CLIP_DURATION_SEC,
            "max_absolute_snap_shift_sec": 0.0,
            "mean_absolute_snap_shift_sec": 0.0,
            "max_absolute_scene_error_sec": 0.0,
            "mean_absolute_scene_error_sec": 0.0,
        },
        "evidence": {
            "root_manifest_exists": True,
            "clip_manifest_exists": True,
            "manifest_events_represented": True,
            "manifest_plan_identity_match": True,
            "artifact_hashes_match": True,
            "required_artifacts_exist": True,
            "stem_format_ok": True,
            "mixed_format_ok": True,
            "manifest_measurements_crosschecked": True,
            "manual_timing_absent": True,
            "isolation": {"passed": True},
        },
    }
    profile["gates"] = harness.evaluate_profile_gates(profile)
    profile["passed"] = profile["gates"]["passed"]
    assert profile["passed"] is True
    return profile


def _healthy_profiles():
    shared = (("event-a", "a", 2.0), ("event-b", "b", 4.5))
    baseline = _profile("baseline", harness.BASELINE_USAGE, shared)
    candidate = _profile(
        "candidate",
        harness.CANDIDATE_USAGE,
        shared + (("event-c", "c", 7.0),),
    )
    return baseline, candidate


def test_fixture_timeline_plan_isolation_audibility_selection_offline(real_evaluation):
    run = real_evaluation
    fixture = run["fixture"]
    root = Path(fixture["root"])
    regular = [path for path in root.rglob("*") if path.is_file()]
    assert len([path for path in regular if path.suffix.lower() == ".mp4"]) == 1
    assert len([path for path in regular if path.suffix.lower() == ".wav"]) == 3
    assert len(regular) == 4
    assert all(not path.is_symlink() for path in regular)
    assert abs(fixture["duration_sec"] - 7.0) <= 0.05
    assert abs(fixture["start_time_sec"]) <= 0.05
    assert all(
        abs(actual - expected) <= 0.05
        for actual, expected in zip(
            fixture["visual_transition_times_sec"],
            harness.EXPECTED_SCENE_CLIP_TIMES,
        )
    )
    assert all(
        abs(item["onset_clip_time_sec"] - item["target_clip_time_sec"]) <= 0.05
        and abs(item["peak_clip_time_sec"] - item["target_clip_time_sec"]) <= 0.05
        for item in fixture["source_audio_evidence"]
    )

    assert run["delivery_call_count"] == 2
    assert tuple(call["profile"] for call in run["delivery_calls"]) == (
        "baseline",
        "candidate",
    )
    assert tuple(call["usage_percent"] for call in run["delivery_calls"]) == (
        50.0,
        100.0,
    )
    assert run["plan_relationships"]["baseline_count"] == 2
    assert run["plan_relationships"]["candidate_count"] == 3
    assert run["plan_relationships"]["baseline_ordered_subset"] is True
    assert run["staging_evidence"]["ordinary_copies"] is True
    assert run["staging_evidence"]["distinct_file_identities"] is True
    assert run["staging_evidence"]["byte_identical_corresponding_hashes"] is True
    assert run["output_identity_evidence"]["passed"] is True
    assert run["output_identity_evidence"]["all_file_identities_distinct"] is True

    baseline = run["profiles"]["baseline"]
    candidate = run["profiles"]["candidate"]
    assert baseline["passed"] is True
    assert candidate["passed"] is True
    assert baseline["metrics"]["audible_cue_density"] == 2 / 7
    assert candidate["metrics"]["audible_cue_density"] == 3 / 7
    assert all(
        cue["cue_window_audibility"]["audible"] is True
        for profile in (baseline, candidate)
        for cue in profile["measured_cues"]
    )
    assert run["selection"]["selected_profile"] == "candidate"
    assert run["selection"]["candidate_accepted"] is True
    assert all(item["passed"] is True for item in run["selection"]["comparisons"])
    assert run["offline_evidence"] == {
        "network_dns_http_download_attempts": 0,
        "main_imported": False,
        "shell_true_calls": 0,
    }
    assert run["regenerated_created"] is False
    assert run["final_cli_report_emitted"] is False
    assert not (Path(run["output_root"]) / "regenerated").exists()


def test_timeline_uses_authoritative_source_times_once(real_evaluation):
    assert [cue.source_time_sec for cue in harness.AUTHORITATIVE_CUES] == [2.0, 4.5, 7.0]
    assert [cue.semantic_clip_time_sec for cue in harness.AUTHORITATIVE_CUES] == [
        1.0,
        3.5,
        6.0,
    ]
    assert real_evaluation["highlight"]["start_sec"] == 1.0
    assert real_evaluation["highlight"]["end_sec"] == 8.0
    for profile in real_evaluation["profiles"].values():
        for cue in profile["measured_cues"]:
            assert (
                cue["semantic_clip_time_sec"]
                == cue["authoritative_source_time_sec"] - 1.0
            )
            assert cue["final_clip_time_sec"] < 7.0


def test_plan_identities_include_event_category_source_time_and_asset_hash(real_evaluation):
    for profile in real_evaluation["profiles"].values():
        identities = []
        for record in profile["expected_plan"]:
            assert record["event_identity"]
            assert record["category"]
            assert record["authoritative_source_time_sec"] in (2.0, 4.5, 7.0)
            assert len(record["asset_sha256"]) == 64
            identities.append(record["identity"])
        assert len(identities) == len(set(identities))
        assert tuple(identities) == tuple(
            record["identity"] for record in profile["actual_plan"]
        )


def test_plan_explicit_empty_cues_does_not_fallback():
    assert harness.resolve_authoritative_cues({"se_cues": []}) == ()
    assert harness.resolve_authoritative_cues({}) == harness.AUTHORITATIVE_CUES


def test_plan_api_has_no_manual_timing_input(real_evaluation):
    evaluation_parameters = tuple(
        inspect.signature(harness.evaluate_two_profiles).parameters
    )
    selector_parameters = tuple(inspect.signature(harness.select_profile).parameters)
    assert evaluation_parameters == ("output_root",)
    assert selector_parameters == ("baseline", "candidate")
    assert real_evaluation["manual_timing"]["supplied"] is False
    assert real_evaluation["manual_timing"]["baseline_state"] in (None, 0.0)
    assert real_evaluation["manual_timing"]["candidate_state"] in (None, 0.0)


def test_isolation_ledger_and_baseline_write_integrity_are_distinct(real_evaluation):
    ledger = real_evaluation["candidate_access_ledger"]
    denied = [entry for entry in ledger if entry["allowed"] is False]
    assert len(denied) == 1
    assert denied[0]["negative_control"] is True
    assert "baseline" in denied[0]["rule"]
    subprocesses = [
        entry for entry in ledger if entry["operation"] == "subprocess" and entry["allowed"]
    ]
    assert subprocesses
    for entry in subprocesses:
        assert Path(entry["argv"][0]).name.lower() in {
            "ffmpeg",
            "ffmpeg.exe",
            "ffprobe",
            "ffprobe.exe",
        }
        assert entry["shell"] is False
        assert entry["close_fds"] is True
        assert set(entry["environment_keys"]).issubset(
            {"PATH", "TEMP", "TMP", "SystemRoot", "WINDIR", "ComSpec", "PATHEXT"}
        )
    integrity = real_evaluation["baseline_write_integrity"]
    assert integrity["passed"] is True
    assert integrity["before_sha256"] == integrity["after_sha256"]
    assert "write-integrity evidence only" in integrity["statement"]
    assert "not read-isolation evidence" in integrity["statement"]


def test_isolation_guard_denies_parent_enumeration_links_and_network(real_evaluation):
    run_root = Path(real_evaluation["output_root"])
    baseline_root = Path(real_evaluation["delivery_calls"][0]["output_dir"])
    candidate_root = Path(real_evaluation["delivery_calls"][1]["output_dir"])
    candidate = {
        "root": candidate_root,
        "staged": candidate_root / "staged",
        "video": Path(real_evaluation["delivery_calls"][1]["video"]),
        "assets_root": Path(real_evaluation["delivery_calls"][1]["se_folder"]),
        "runtime": candidate_root / "runtime",
    }
    guard = harness.CandidateAccessGuard(
        candidate=candidate,
        baseline_root=baseline_root,
        baseline_inventory=harness._tree_inventory(baseline_root),
        canonical_root=Path(real_evaluation["fixture"]["root"]),
        tools=harness._resolve_media_tools(),
    )
    with guard.active():
        with pytest.raises(harness.IsolationViolation):
            os.listdir(run_root)
        with pytest.raises(harness.IsolationViolation):
            os.link(
                baseline_root / "audio_manifest.json",
                candidate_root / "baseline-alias.json",
            )
        with pytest.raises(harness.IsolationViolation):
            __import__("socket").getaddrinfo("example.invalid", 443)
    assert not (candidate_root / "baseline-alias.json").exists()


def test_audibility_and_media_are_measured_from_real_artifacts(real_evaluation):
    for profile in real_evaluation["profiles"].values():
        assert Path(profile["root_manifest_path"]).is_file()
        assert Path(profile["clip_manifest_path"]).is_file()
        assert profile["evidence"]["artifact_hashes_match"] is True
        assert profile["evidence"]["manifest_measurements_crosschecked"] is True
        assert profile["evidence"]["stem_format_ok"] is True
        assert profile["evidence"]["mixed_format_ok"] is True
        assert profile["metrics"]["decoded_4x_peak_dbfs"] <= -1.0
        assert profile["metrics"]["post_mix_attenuation_db"] >= -3.0
        assert profile["metrics"]["limiter_margin_db"] >= 0.0
        for cue in profile["measured_cues"]:
            evidence = cue["cue_window_audibility"]
            assert evidence["stem_delta_db"] >= 6.0
            assert evidence["mixed_delta_db"] >= 6.0
            assert evidence["stem_onset_error_sec"] <= 0.05


@pytest.mark.parametrize(
    "field",
    (
        "root_manifest_exists",
        "clip_manifest_exists",
        "manifest_events_represented",
        "manifest_plan_identity_match",
        "artifact_hashes_match",
        "required_artifacts_exist",
        "stem_format_ok",
        "mixed_format_ok",
        "manifest_measurements_crosschecked",
        "manual_timing_absent",
    ),
)
def test_gate_fails_each_evidence_family(field):
    _baseline, candidate = _healthy_profiles()
    candidate["evidence"][field] = False
    gate = harness.evaluate_profile_gates(candidate)
    assert gate["passed"] is False
    assert gate["checks"][field] is False


def test_gate_fails_isolation_family():
    _baseline, candidate = _healthy_profiles()
    candidate["evidence"]["isolation"]["passed"] = False
    gate = harness.evaluate_profile_gates(candidate)
    assert gate["passed"] is False
    assert gate["checks"]["profile_isolation"] is False


@pytest.mark.parametrize(
    ("family", "value"),
    (
        ("shift", math.nextafter(0.75, math.inf)),
        ("scene", math.nextafter(0.5, math.inf)),
        ("audibility", math.nextafter(6.0, -math.inf)),
        ("snap_peak", math.nextafter(0.55, -math.inf)),
        ("decoded_peak", math.nextafter(-1.0, math.inf)),
        ("attenuation", math.nextafter(-3.0, -math.inf)),
        ("limiter", math.nextafter(0.0, -math.inf)),
    ),
)
def test_gate_zero_tolerance_boundaries_fail_closed(family, value):
    _baseline, candidate = _healthy_profiles()
    if family == "shift":
        candidate["measured_cues"][0]["absolute_snap_shift_sec"] = value
    elif family == "scene":
        candidate["measured_cues"][0]["absolute_expected_scene_error_sec"] = value
    elif family == "audibility":
        candidate["measured_cues"][0]["cue_window_audibility"]["stem_delta_db"] = value
    elif family == "snap_peak":
        candidate["measured_cues"][0]["snapped"] = True
        candidate["measured_cues"][0]["excitement_peak_score"] = value
    elif family == "decoded_peak":
        candidate["metrics"]["decoded_4x_peak_dbfs"] = value
    elif family == "attenuation":
        candidate["metrics"]["post_mix_attenuation_db"] = value
    elif family == "limiter":
        candidate["metrics"]["limiter_margin_db"] = value
    gate = harness.evaluate_profile_gates(candidate)
    assert gate["passed"] is False


def test_gate_rejects_plan_identity_changes():
    _baseline, candidate = _healthy_profiles()
    candidate["actual_plan"][0]["asset_sha256"] = "f" * 64
    gate = harness.evaluate_profile_gates(candidate)
    assert gate["passed"] is False
    assert gate["checks"]["plan_identity_exact"] is False


@pytest.mark.parametrize("bad_value", ("-2", None, math.nan, math.inf, -math.inf))
def test_gate_rejects_numeric_type_missing_nan_and_infinity(bad_value):
    _baseline, candidate = _healthy_profiles()
    if bad_value is None:
        candidate["metrics"].pop("decoded_4x_peak_dbfs")
    else:
        candidate["metrics"]["decoded_4x_peak_dbfs"] = bad_value
    assert harness.evaluate_profile_gates(candidate)["passed"] is False


def test_selection_happy_path_is_strict_density_and_raw_nonregression():
    baseline, candidate = _healthy_profiles()
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "candidate"
    assert selected["candidate_accepted"] is True
    assert all(item["passed"] is True for item in selected["comparisons"])


def test_selection_baseline_gate_failure_selects_nothing():
    baseline, candidate = _healthy_profiles()
    baseline["gates"] = {"passed": False, "reasons": ("injected baseline failure",)}
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] is None
    assert selected["run_passed"] is False


def test_selection_candidate_gate_failure_falls_back_with_field_reason():
    baseline, candidate = _healthy_profiles()
    candidate["gates"] = {"passed": False, "reasons": ("cue gate",)}
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert selected["run_passed"] is True
    assert any("cue gate" in reason for reason in selected["reasons"])


@pytest.mark.parametrize(
    "field",
    harness._NONINCREASING_METRICS,
)
def test_selection_rejects_nextafter_increase_for_every_raw_aggregate(field):
    baseline, candidate = _healthy_profiles()
    candidate["metrics"][field] = math.nextafter(
        baseline["metrics"][field], math.inf
    )
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any(field in reason for reason in selected["reasons"])


@pytest.mark.parametrize(
    "field",
    harness._NONDECREASING_METRICS,
)
def test_selection_rejects_nextafter_decrease_for_every_raw_aggregate(field):
    baseline, candidate = _healthy_profiles()
    candidate["metrics"][field] = math.nextafter(
        baseline["metrics"][field], -math.inf
    )
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any(field in reason for reason in selected["reasons"])


@pytest.mark.parametrize(
    "field",
    ("absolute_expected_scene_error_sec", "absolute_snap_shift_sec"),
)
def test_selection_rejects_nextafter_shared_cue_regression(field):
    baseline, candidate = _healthy_profiles()
    candidate["measured_cues"][0][field] = math.nextafter(
        baseline["measured_cues"][0][field], math.inf
    )
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any(field in reason for reason in selected["reasons"])


def test_selection_rejects_missing_shared_plan_identity():
    baseline, candidate = _healthy_profiles()
    candidate["measured_cues"] = candidate["measured_cues"][1:]
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any("missing shared identity" in reason for reason in selected["reasons"])


@pytest.mark.parametrize("bad_value", (math.nan, math.inf, -math.inf))
def test_selection_rejects_nan_and_infinity(bad_value):
    baseline, candidate = _healthy_profiles()
    candidate["metrics"]["decoded_4x_peak_dbfs"] = bad_value
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert selected["candidate_accepted"] is False


def test_selection_rejects_missing_value_and_numeric_type_mismatch():
    baseline, candidate = _healthy_profiles()
    candidate["metrics"].pop("limiter_margin_db")
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    baseline, candidate = _healthy_profiles()
    candidate["metrics"]["decoded_4x_peak_dbfs"] = -2
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any("type differs" in reason for reason in selected["reasons"])


def test_selection_requires_strictly_greater_density():
    baseline, candidate = _healthy_profiles()
    candidate["metrics"]["audible_cue_density"] = baseline["metrics"][
        "audible_cue_density"
    ]
    selected = harness.select_profile(baseline, candidate)
    assert selected["selected_profile"] == "baseline"
    assert any("strictly greater" in reason for reason in selected["reasons"])


def test_complete_loop_performs_fresh_third_regeneration(complete_runs):
    run = complete_runs[0]
    assert run["delivery_call_count"] == 3
    assert tuple(call["profile"] for call in run["delivery_calls"]) == (
        "baseline",
        "candidate",
        "regenerated",
    )
    assert run["regeneration"]["selected_profile"] == "candidate"
    assert run["regeneration"]["selected_usage_percent"] == 100.0
    assert run["regeneration"]["fresh_tree_pre_call"] is True
    assert run["regeneration"]["pre_call_file_count"] == 4
    assert run["regeneration"]["projection_equal"] is True
    assert run["regeneration"]["passed"] is True
    assert run["profiles"]["regenerated"]["passed"] is True
    assert run["regenerated_created"] is True
    assert run["final_cli_report_emitted"] is True
    assert Path(run["report_path"]).is_file()
    assert not list(Path(run["output_root"]).glob(".*.tmp"))
    assert run["report"]["run_passed"] is True
    assert run["report"]["manual_timing"]["supplied"] is False


def test_two_fresh_complete_runs_have_equal_deterministic_reports(complete_runs):
    first, second = complete_runs
    forward = harness.compare_reports(first["report_path"], second["report_path"])
    reverse = harness.compare_reports(second["report_path"], first["report_path"])
    assert forward["passed"] is True
    assert reverse["passed"] is True
    assert forward["first_projection_sha256"] == forward["second_projection_sha256"]
    assert reverse["first_projection_sha256"] == reverse["second_projection_sha256"]


def test_complete_loop_cli_exposes_no_manual_seconds_option():
    assert tuple(inspect.signature(harness.run_tuning_loop).parameters) == (
        "output_root",
    )
    help_text = harness._build_cli_parser().format_help().casefold()
    assert "--seconds" not in help_text
    assert "--timing" not in help_text
    assert "--cue" not in help_text
    destinations = {
        action.dest for action in harness._build_cli_parser()._actions
    }
    assert destinations == {"help", "output_dir", "compare_reports"}
