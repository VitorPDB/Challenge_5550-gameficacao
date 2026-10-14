"""Bounded offline replay using actual detectors; never writes operational alerts."""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path

import cv2

from app.config import BASE_DIR
from app.extensions import db
from app.models import Camera, ScenarioRun
from app.services.prevention_service import PreventionService, draw_prevention


def scenario_dir(app):
    return Path(app.config.get("SCENARIO_DIR", Path(BASE_DIR)/"runtime"/"scenarios"))


class ScenarioMetrics:
    def __init__(self, horizon):
        self.horizon = horizon
        self.previous_inside = set()
        self.previous_approaching = set()
        self.entries = []
        self.warnings = []
        self.last_at = 0

    def observe(self, state, now):
        inside, approaching = set(), set()
        for person in state["people"]:
            for zone in person["zones"]:
                key = (person["person_id"], zone["zone_id"])
                if zone["inside"]:
                    inside.add(key)
                    if key not in self.previous_inside:
                        self.entries.append({"person_id": key[0], "zone_id": key[1], "at_s": round(now, 3)})
                if zone["approaching"]:
                    approaching.add(key)
                    if key not in self.previous_approaching:
                        self.warnings.append({"person_id": key[0], "zone_id": key[1], "at_s": round(now, 3),
                                              "eta_s": zone["eta_s"]})
        self.previous_inside, self.previous_approaching = inside, approaching
        self.last_at = now

    def result(self):
        matched = []
        for entry in self.entries:
            warnings = [w for w in self.warnings if (w["person_id"], w["zone_id"]) == (entry["person_id"], entry["zone_id"])
                        and 0 < entry["at_s"]-w["at_s"] <= self.horizon+0.25]
            if warnings:
                matched.append(round(entry["at_s"]-min(w["at_s"] for w in warnings), 3))
        false_warnings = sum(not any(
            (w["person_id"], w["zone_id"]) == (e["person_id"], e["zone_id"])
            and 0 < e["at_s"]-w["at_s"] <= self.horizon+0.25 for e in self.entries)
            for w in self.warnings if w["at_s"]+self.horizon+0.25 <= self.last_at)
        censored = sum(w["at_s"]+self.horizon+0.25 > self.last_at for w in self.warnings)
        return {"entries": self.entries, "warnings": self.warnings, "anticipated_entries": len(matched),
                "lead_times_s": matched, "unconfirmed_warning_episodes": false_warnings, "censored_warnings": censored,
                "basis": "Detector/tracker observations, not manually annotated ground truth; no model precision claimed."}


class ScenarioService:
    def __init__(self, app):
        self.app = app
        self._slot = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress = {}

    def recover_interrupted(self):
        # Single process deployment: any persisted running job at boot belongs to a previous process.
        runs = ScenarioRun.query.filter(ScenarioRun.status.in_(["queued", "running"])).all()
        for run in runs:
            run.status, run.error = "failed", "Processamento interrompido pelo reinício do servidor. Envie o cenário novamente."
            (scenario_dir(self.app)/run.id/"source.mp4").unlink(missing_ok=True)
        db.session.commit()

    def reserve(self):
        return self._slot.acquire(blocking=False)

    def release(self):
        self._slot.release()

    def progress(self, run_id):
        with self._progress_lock:
            return dict(self._progress.get(run_id, {}))

    def start(self, run_id, configs):
        thread = threading.Thread(target=self._run, args=(run_id, configs), daemon=True, name="scenario-replay")
        thread.start()

    def _run(self, run_id, configs):
        directory = scenario_dir(self.app)/run_id
        capture = None
        with self.app.app_context():
            try:
                run = db.session.get(ScenarioRun, run_id)
                camera = db.session.get(Camera, run.camera_id)
                if camera is None:
                    raise ValueError("A câmera foi removida.")
                # A separate worker owns tracking/caches; only immutable model resources and their lock are shared.
                worker = self.app.extensions["monitor_service"]._build_worker(camera)
                worker.detect_every_n = 1
                engines = [PreventionService(config) for config in configs]
                counters = [ScenarioMetrics(config["horizon_s"]) for config in configs]
                run.status = "running"
                db.session.commit()
                capture = cv2.VideoCapture(str(directory/"source.mp4"))
                if not capture.isOpened():
                    raise ValueError("Não foi possível abrir o vídeo enviado.")
                fps = float(capture.get(cv2.CAP_PROP_FPS))
                if not math.isfinite(fps) or not 1 <= fps <= 240:
                    raise ValueError("O vídeo precisa informar FPS válido (1 a 240).")
                stride = max(1, math.ceil(fps/5))
                frame_index, analyzed, started, samples = 0, 0, time.monotonic(), []
                while analyzed < 600 and frame_index/fps <= 120 and time.monotonic()-started < 600:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    timestamp = frame_index/fps
                    frame_index += 1
                    if (frame_index-1) % stride:
                        continue
                    frame = worker._aplicar_rotacao(frame)
                    if max(frame.shape[:2]) > 1280:
                        scale = 1280/max(frame.shape[:2])
                        frame = cv2.resize(frame, (int(frame.shape[1]*scale), int(frame.shape[0]*scale)))
                    analysis = worker._analyze_frame(frame)
                    evaluation = worker.rule_engine.analyze(analysis.detections, analysis.pose, frame.shape, poses=analysis.poses)
                    states = []
                    for engine, counter in zip(engines, counters, strict=True):
                        state, _alerts = engine.process(evaluation.people, evaluation.alerts, frame, timestamp,
                                                       risk_enabled=worker.feature_manager.is_enabled("risk_area"),
                                                       posture_enabled=worker.feature_manager.is_enabled("pose")
                                                       and worker.feature_manager.is_enabled("posture"))
                        counter.observe(state, timestamp)
                        states.append(state)
                    if analyzed % 10 == 0 and len(samples) < 60:
                        filename = f"frame-{analyzed}.jpg"
                        if not cv2.imwrite(str(directory/filename), draw_prevention(frame.copy(), states[0])):
                            raise ValueError("Falha ao salvar evidência do cenário.")
                        samples.append({"at_s": round(timestamp, 3), "image": filename, "states": states})
                    analyzed += 1
                    with self._progress_lock:
                        self._progress[run_id] = {"analyzed_frames": analyzed, "video_time_s": round(timestamp, 3)}
                if not analyzed:
                    raise ValueError("Vídeo sem frames decodificáveis.")
                result = {"mode": "offline_lab", "configs": configs, "analyzed_frames": analyzed,
                          "sample_fps": fps/stride, "runtime_s": round(time.monotonic()-started, 2),
                          "video_time_s": round(timestamp, 3), "truncated": analyzed >= 600 or timestamp >= 120
                          or time.monotonic()-started >= 600,
                          "comparisons": [counter.result() for counter in counters], "samples": samples}
                (directory/"report.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                run = db.session.get(ScenarioRun, run_id)
                run.result_json, run.status = result, "completed"
                db.session.commit()
            except Exception:
                db.session.rollback()
                self.app.logger.exception("scenario_replay_failed")
                run = db.session.get(ScenarioRun, run_id)
                if run:
                    run.status, run.error = "failed", "Falha no processamento. Verifique vídeo, pesos e diagnóstico no servidor."
                    db.session.commit()
            finally:
                if capture:
                    capture.release()
                (directory/"source.mp4").unlink(missing_ok=True)
                with self._progress_lock:
                    self._progress.pop(run_id, None)
                db.session.remove()
                self.release()
