from types import SimpleNamespace

import cv2
import numpy as np
from app.extensions import db
from app.models import Alert, Camera, EventLog, ScenarioRun
from app.services.feature_manager import FeatureManager
from app.services.prevention_service import validate_config
from app.services.risk_rules import RuleEngine
from app.services.scenario_service import ScenarioMetrics, scenario_dir
from app.vision.person_tracker import PersonTracker
from app.vision.schemas import BoundingBox, Detection, FrameAnalysis


def test_metrics_exclude_censored_warnings_and_measure_lead_time():
    counter = ScenarioMetrics(2)
    def state(inside=False, approaching=False):
        return {"people": [{"person_id": "person_1", "zones": [
            {"zone_id": "press", "inside": inside, "approaching": approaching, "eta_s": 1}]}]}
    counter.observe(state(approaching=True), 1)
    counter.observe(state(inside=True), 2)
    counter.observe(state(), 4)
    counter.observe(state(approaching=True), 5)
    result = counter.result()
    assert result["anticipated_entries"] == 1
    assert result["lead_times_s"] == [1]
    assert result["unconfirmed_warning_episodes"] == 0
    assert result["censored_warnings"] == 1


def test_actual_video_decoder_replay_is_isolated_from_operational_history(real_monitor_app, monkeypatch, tmp_path):
    app = real_monitor_app
    app.config["SCENARIO_DIR"] = str(tmp_path)
    camera = Camera(name="Laboratório", source="0", source_type="USB")
    db.session.add(camera)
    db.session.commit()
    identifier = "a"*32
    directory = scenario_dir(app)/identifier
    directory.mkdir()
    writer = cv2.VideoWriter(str(directory/"source.mp4"), cv2.VideoWriter_fourcc(*"mp4v"), 10, (640, 480))
    assert writer.isOpened()
    frame = np.random.default_rng(7).integers(30, 200, (480, 640, 3), dtype=np.uint8)
    for _ in range(50):
        writer.write(frame)
    writer.release()
    db.session.add(ScenarioRun(id=identifier, camera_id=camera.id))
    db.session.commit()
    features = FeatureManager.from_camera_features({"ppe": True, "risk_area": True})
    rules = RuleEngine(features, 1, [])
    tracker = PersonTracker()
    observed = 0
    def analyze(_frame):
        nonlocal observed
        x = 100+observed*12
        observed += 1
        detections = tracker.update([Detection("person", .95, BoundingBox(x-40, 80, x+40, 300), category="person")])
        return FrameAnalysis(detections, None, [])
    worker = SimpleNamespace(detect_every_n=1, rule_engine=rules, feature_manager=features,
                             _aplicar_rotacao=lambda frame: frame, _analyze_frame=analyze)
    monkeypatch.setattr(app.extensions["monitor_service"], "_build_worker", lambda _camera: worker)
    config = validate_config({"zones": [{"id": "press", "name": "Prensa", "polygon": [[.55, .2], [.8, .2], [.8, .9], [.55, .9]]}]})
    service = app.extensions["scenario_service"]
    assert service.reserve()
    service._run(identifier, [config, {**config, "horizon_s": 3}])
    run = db.session.get(ScenarioRun, identifier)
    assert run.status == "completed", run.error
    assert run.result_json["analyzed_frames"] == 25
    assert len(run.result_json["comparisons"]) == 2
    assert run.result_json["comparisons"][0]["anticipated_entries"] >= 1
    assert (directory/"report.json").exists()
    assert (directory/run.result_json["samples"][0]["image"]).exists()
    assert not (directory/"source.mp4").exists()
    assert Alert.query.count() == EventLog.query.count() == 0
    assert service.reserve()
    service.release()
