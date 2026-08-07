from types import SimpleNamespace

from se_auto import normalise_se_usage, plan_se_assignments


def _highlights(count=5):
    return tuple(
        {
            "title": f"clip-{index}",
            "start_sec": index * 10,
            "end_sec": index * 10 + 5,
        }
        for index in range(count)
    )


def test_normalise_se_usage_clamps_invalid_values():
    assert normalise_se_usage(-1) == 0
    assert normalise_se_usage(125) == 100
    assert normalise_se_usage("not-a-number", default=35) == 35
    assert normalise_se_usage(float("nan"), default=35) == 35


def test_se_plan_uses_folder_order_and_slider_density():
    assets = tuple(SimpleNamespace(filename=f"se-{index}.mp3") for index in range(3))

    off = plan_se_assignments(assets, _highlights(), 0)
    assert all(not assignment.enabled for assignment in off)

    half = plan_se_assignments(assets, _highlights(), 50, cue_seconds=1.25)
    assert sum(assignment.enabled for assignment in half) == 3
    assert all(
        assignment.cue_seconds == 1.25
        for assignment in half
        if assignment.enabled
    )
    assigned = [assignment.asset.filename for assignment in half if assignment.enabled]
    assert set(assigned) <= {"se-0.mp3", "se-1.mp3", "se-2.mp3"}
    assert assigned == [
        assets[index % len(assets)].filename
        for index, assignment in enumerate(half)
        if assignment.enabled
    ]


def test_se_plan_is_reproducible_and_does_not_always_start_at_first_clip():
    assets = tuple(SimpleNamespace(filename=f"se-{index}.mp3") for index in range(2))
    highlights = _highlights()

    first = plan_se_assignments(assets, highlights, 40)
    second = plan_se_assignments(assets, highlights, 40)

    assert first == second
    assert any(not assignment.enabled for assignment in first)
