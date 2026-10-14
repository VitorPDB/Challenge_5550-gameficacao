from copy import deepcopy

import numpy as np
import pytest
from app.services.prevention_service import PreventionService, entry_fraction, validate_config
from app.services.risk_rules import RuleAlert

ZONE = {"id": "press", "name": "Prensa", "polygon": [[0.6, 0.1], [0.7, 0.1], [0.7, 0.9], [0.6, 0.9]],
        "required_ppe": ["helmet"], "restricted": True}
FRAME = np.random.default_rng(12).integers(30, 220, (500, 1000, 3), dtype=np.uint8)


def person(x, identifier="person_1", helmet="ok"):
    return {"id": identifier, "box": {"x1": int(x*1000)-40, "x2": int(x*1000)+40, "y1": 100, "y2": 300},
            "ppe": {"helmet": {"status": helmet}}}


def test_segment_crosses_narrow_zone_even_with_endpoint_beyond_it():
    assert entry_fraction((0.1, 0.5), (0.9, 0.5), ZONE["polygon"]) == pytest.approx(0.625)
    assert entry_fraction((0.1, 0.95), (0.9, 0.95), ZONE["polygon"]) is None


def test_projection_warns_before_observed_entry_and_explains_ppe():
    service = PreventionService({"zones": [ZONE]})
    for i in range(8):
        state, alerts = service.process([person(0.2+i*.025, helmet="unsupported")], [], FRAME, i*.2)
    assert state["people"][0]["zones"][0]["approaching"]
    assert 0 < state["people"][0]["zones"][0]["eta_s"] <= 2
    assert alerts[0].metadata["unconfirmed_ppe"] == ["helmet"]
    assert alerts[0].metadata["basis"] == "constant_velocity_kalman"
    _, alerts = service.process([person(.625)], [], FRAME, 1.6)
    assert any(a.rule == "zone_entry:press" for a in alerts)


def test_stationary_or_parallel_person_does_not_predict_entry():
    for stationary in (True, False):
        service = PreventionService({"zones": [ZONE]})
        for i in range(10):
            p = person(.2 if stationary else .2+i*.025)
            p["box"]["y2"] = 490
            state, alerts = service.process([p], [], FRAME, i*.2)
        assert not any(a.rule.startswith("predicted_zone_entry") for a in alerts)


def test_reused_frames_never_add_history_or_exposure():
    service = PreventionService({"exposure_threshold_s": 1})
    bad = [RuleAlert("suspicious_posture", "medium", "Postura", "posture", {"person_id": "person_1"})]
    service.process([person(.2)], bad, FRAME, 0)
    state, _ = service.process([person(.2)], bad, FRAME, 1)
    before = deepcopy(state)
    state, alerts = service.process([person(.2)], bad, FRAME, 20, fresh=False)
    assert state["people"] == before["people"]
    assert state["observation_reused"]
    assert alerts == []


def test_lost_person_and_long_gap_reset_motion_and_exposure():
    service = PreventionService({})
    for i in range(8):
        service.process([person(.2+i*.02)], [], FRAME, i*.2)
    state, _ = service.process([], [], FRAME, 1.6)
    assert state["people"] == []
    state, _ = service.process([person(.4)], [], FRAME, 1.8)
    assert state["people"][0]["projection"] is None
    state, _ = service.process([person(.4)], [], FRAME, 20)
    assert state["people"][0]["projection"] is None


def test_exposure_is_per_person_and_requires_current_bad_posture():
    service = PreventionService({"exposure_threshold_s": 1, "exposure_window_s": 10})
    bad = [RuleAlert("suspicious_posture", "medium", "Postura", "posture", {"person_id": "person_1"})]
    for i in range(7):
        state, alerts = service.process([person(.2), person(.8, "person_2")], bad, FRAME, i*.2)
    assert state["people"][0]["exposure_s"] == pytest.approx(1.2)
    assert state["people"][1]["exposure_s"] == 0
    assert any(a.rule == "accumulated_posture" for a in alerts)
    _, alerts = service.process([person(.2)], [], FRAME, 1.4)
    assert not any(a.rule == "accumulated_posture" for a in alerts)
    service.process([], [], FRAME, 1.6)
    state, _ = service.process([person(.2)], bad, FRAME, 1.8)
    assert state["people"][0]["exposure_s"] == 0


def test_only_converging_people_generate_encounter_warning():
    service = PreventionService({})
    for i in range(8):
        state, alerts = service.process([person(.2+i*.02), person(.8-i*.02, "person_2")], [], FRAME, i*.2)
    assert state["encounters"]
    assert any(a.rule == "converging_people" for a in alerts)
    service = PreventionService({})
    for i in range(8):
        state, _ = service.process([person(.4-i*.02), person(.6+i*.02, "person_2")], [], FRAME, i*.2)
    assert state["encounters"] == []


def test_coverage_debounces_and_does_not_claim_obstruction_confirmed():
    service = PreventionService({})
    dark = np.zeros_like(FRAME)
    state, alerts = service.process([], [], dark, 0)
    assert state["coverage"]["status"] == "suspect"
    assert not alerts
    state, alerts = service.process([], [], dark, 1.1)
    assert any(a.rule == "coverage_degraded" for a in alerts)
    assert state["coverage"]["obstruction_confirmed"] is False
    state, alerts = service.process([], [], FRAME, 1.3)
    assert state["coverage"]["status"] == "usable"
    assert not alerts


@pytest.mark.parametrize("config", [None, [], {"horizon_s": float("nan")}, {"enabled": "false"},
                                   {"zones": [{**ZONE, "polygon": [[0, 0], [1, 1], [1, 1]]}]},
                                   {"zones": [ZONE, ZONE]}, {"zones": [{**ZONE, "required_ppe": ["banana"]}]},
                                   {"exposure_window_s": 10, "exposure_threshold_s": 20}])
def test_invalid_configuration_is_rejected(config):
    with pytest.raises(ValueError):
        validate_config(config)


def test_switches_disable_corresponding_rules():
    service = PreventionService({"zones": [ZONE], "trajectory": False, "proximity": False, "coverage": False})
    for i in range(8):
        state, alerts = service.process([person(.2+i*.02)], [], FRAME, i*.2, risk_enabled=False)
    assert state["people"][0]["zones"] == []
    assert state["people"][0]["projection"] is None
    assert alerts == []
