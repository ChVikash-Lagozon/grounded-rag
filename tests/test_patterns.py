"""Every planted pattern must be detectable with SQL on the generated history."""

from __future__ import annotations

import pytest

from carquery.datafiles import connect_views
from carquery.generator.aftersales import recall_campaigns
from carquery.generator.patterns import build_patterns, measure
from carquery.generator.world import build_world

from .conftest import Dataset

PATTERNS = [
    "p1_brake_batch",
    "p2_bev_share",
    "p3_seasonality",
    "p4_slow_dealers",
    "p5_line_change",
    "p6_fleet",
    "p7_battery_wear",
    "p8_regional_repairs",
]


@pytest.fixture(scope="module")
def measured(dataset: Dataset) -> dict[str, dict]:
    world = build_world(dataset.generator, dataset.reference)
    patterns = build_patterns(world, recall_campaigns(world))
    con = connect_views(dataset.root, dataset.contract)
    return {p.key: measure(con, p) for p in patterns}


def test_all_patterns_have_detection_queries(measured: dict[str, dict]) -> None:
    assert sorted(measured) == PATTERNS


@pytest.mark.parametrize("key", PATTERNS)
def test_pattern_is_detected(key: str, measured: dict[str, dict]) -> None:
    metrics = measured[key]
    assert metrics["detected"] is True, metrics


def test_brake_batch_effect_is_strong(measured: dict[str, dict]) -> None:
    p1 = measured["p1_brake_batch"]
    assert p1["affected_claims_per_1000"] >= 5 * p1["other_claims_per_1000"]


def test_patterns_do_not_leak_into_controls(measured: dict[str, dict]) -> None:
    p8 = measured["p8_regional_repairs"]
    # the same model elsewhere and other models in the region look alike
    ratio = p8["same_model_elsewhere_rate"] / p8["same_region_other_models_rate"]
    assert 0.6 < ratio < 1.6
    p5 = measured["p5_line_change"]
    assert abs(p5["other_plants_after"] - p5["pass_rate_before"]) < 0.03
