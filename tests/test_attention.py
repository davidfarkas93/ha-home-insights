"""Differential contract and safety regressions against the frozen TS oracle."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.home_insights.attention import attention_snapshot
from custom_components.home_insights.attention_runtime import read_only_script

from .attention_cases import cases

CASES = cases()
EXPECTED = json.loads(Path(__file__).with_name("attention_expected.json").read_text())["expected"]


@pytest.mark.parametrize("name", CASES)
def test_frozen_luma_contract(name):
    case = CASES[name]
    assert attention_snapshot(**case) == EXPECTED[name]


def test_household_reader_rejects_control_scripts():
    assert read_only_script(
        SimpleNamespace(
            raw_config={
                "sequence": [{"variables": {"x": 1}}, {"stop": "done", "response_variable": "x"}]
            }
        )
    )
    for step in (
        {"action": "switch.turn_on"},
        {"event": "example"},
        {"parallel": []},
        {"delay": 1},
    ):
        assert not read_only_script(SimpleNamespace(raw_config={"sequence": [step]}))
