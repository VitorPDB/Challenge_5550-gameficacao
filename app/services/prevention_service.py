"""Short-horizon image-space prevention. No inference, identity or metres invented."""
from __future__ import annotations

import math
from collections import deque
from copy import deepcopy
from dataclasses import dataclass, field
from itertools import combinations
from threading import RLock

import cv2
import numpy as np

from app.services.risk_rules import RuleAlert
from app.utils.geometry import point_in_polygon
from app.vision.person_compliance_matcher import PPE_KEYS

DEFAULT_PREVENTION = {
    "enabled": True, "trajectory": True, "proximity": True, "ergonomics": True, "coverage": True,
    "horizon_s": 2.0, "max_gap_s": 1.5, "min_history_s": 0.4,
    "proximity_ratio": 0.45, "exposure_threshold_s": 30.0, "exposure_window_s": 300.0,
    "zones": [],
}


def validate_config(raw):
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_PREVENTION):
        raise ValueError("Configuração de prevenção inválida ou com campos desconhecidos.")
    result = deepcopy(DEFAULT_PREVENTION)
    result.update(deepcopy(raw))
    for key in ("enabled", "trajectory", "proximity", "ergonomics", "coverage"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key}: informe true ou false.")
    bounds = {"horizon_s": (0.2, 5), "max_gap_s": (0.2, 5), "min_history_s": (0.2, 3),
              "proximity_ratio": (0.05, 2), "exposure_threshold_s": (1, 600), "exposure_window_s": (10, 1800)}
    for key, (low, high) in bounds.items():
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key}: esperado número entre {low} e {high}.")
    if result["exposure_threshold_s"] > result["exposure_window_s"]:
        raise ValueError("O limiar de exposição não pode superar a janela.")
    zones = result["zones"]
    if not isinstance(zones, list) or len(zones) > 12:
        raise ValueError("Informe até 12 zonas.")
    ids = set()
    for zone in zones:
        if not isinstance(zone, dict) or set(zone) - {"id", "name", "polygon", "required_ppe", "restricted"}:
            raise ValueError("Zona inválida.")
        identifier = zone.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 40 or identifier in ids:
            raise ValueError("Cada zona precisa de um id único de até 40 caracteres.")
        ids.add(identifier)
        name = zone.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Informe um nome para cada zona (até 80 caracteres).")
        polygon = zone.get("polygon")
        if not isinstance(polygon, list) or not 3 <= len(polygon) <= 32:
            raise ValueError("Polígono: informe de 3 a 32 pontos [x,y], entre 0 e 1.")
        for point in polygon:
            if not isinstance(point, list) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in point
            ):
                raise ValueError("Coordenadas devem ser números finitos entre 0 e 1.")
        area = sum(polygon[i][0] * polygon[(i+1) % len(polygon)][1]
                   - polygon[(i+1) % len(polygon)][0] * polygon[i][1] for i in range(len(polygon)))
        if abs(area) < 1e-6:
            raise ValueError("A zona precisa de área não nula.")
        required = zone.setdefault("required_ppe", [])
        if not isinstance(required, list) or any(not isinstance(k, str) or k not in PPE_KEYS for k in required):
            raise ValueError("EPI desconhecido na zona.")
        if len(set(required)) != len(required):
            raise ValueError("EPIs duplicados na zona.")
        zone.setdefault("restricted", True)
        if type(zone["restricted"]) is not bool:
            raise ValueError("restricted: informe true ou false.")
    return result


def entry_fraction(start, end, polygon):
    """Exact segment intersections, including narrow zones; tangencies do not enter."""
    if point_in_polygon(start, polygon):
        return 0.0
    direction = np.asarray(end) - start
    cuts = [0.0, 1.0]
    for a, b in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        edge = np.asarray(b) - a
        determinant = direction[0] * edge[1] - direction[1] * edge[0]
        if abs(determinant) < 1e-12:
            continue
        offset = np.asarray(a) - start
        t = (offset[0] * edge[1] - offset[1] * edge[0]) / determinant
        u = (offset[0] * direction[1] - offset[1] * direction[0]) / determinant
        if 0 <= t <= 1 and 0 <= u <= 1:
            cuts.append(float(t))
    cuts = sorted(set(cuts))
    for left, right in zip(cuts, cuts[1:]):
        middle = np.asarray(start) + direction * ((left + right) / 2)
        if right-left > 1e-9 and point_in_polygon(middle, polygon):
            return left
    return 1.0 if point_in_polygon(end, polygon) else None


@dataclass
class Motion:
    filter: object
    last_at: float
    history: deque = field(default_factory=lambda: deque(maxlen=40))
    exposure: deque = field(default_factory=deque)
    was_bad: bool = False
    residual: float = 0.0


class PreventionService:
    def __init__(self, config=None):
        self._lock = RLock()
        self.configure(config or {})

    def configure(self, config):
        with self._lock:
            self.config = validate_config(config)
            self._tracks = {}
            self._last = {"people": [], "zones": [], "encounters": [], "coverage": {"status": "waiting"}}
            self._coverage_candidate = None
            self._coverage_since = None

    def reset(self):
        self.configure(self.config)

    @staticmethod
    def _new_motion(point, now):
        kalman = cv2.KalmanFilter(4, 2, 0, cv2.CV_64F)
        kalman.measurementMatrix = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype=float)
        kalman.measurementNoiseCov = np.eye(2) * 0.000025
        kalman.errorCovPost = np.diag([0.000025, 0.000025, 0.1, 0.1])
        kalman.statePost = np.array([[point[0]], [point[1]], [0], [0]], dtype=float)
        motion = Motion(kalman, now)
        motion.history.append((now, *point))
        return motion

    def process(self, people, rule_alerts, frame, now, *, fresh=True, risk_enabled=True, posture_enabled=True):
        with self._lock:
            if not fresh:
                # Do not publish an old prediction as a new observation.
                result = deepcopy(self._last)
                result["observation_reused"] = True
                return result, []
            if not self.config["enabled"]:
                return {"enabled": False, "people": [], "zones": [], "encounters": [], "coverage": {"status": "disabled"}}, []
            height, width = frame.shape[:2]
            seen = set()
            output, alerts = [], []
            bad = {a.metadata.get("person_id") for a in rule_alerts if a.rule == "suspicious_posture"}
            for person in people:
                identifier = person["id"]
                seen.add(identifier)
                box = person["box"]
                point = ((box["x1"] + box["x2"]) / (2*width), box["y2"] / height)
                motion = self._tracks.get(identifier)
                dt = now-motion.last_at if motion else 0
                if motion is None or dt > self.config["max_gap_s"] or dt <= 0:
                    motion = self._new_motion(point, now)
                    self._tracks[identifier] = motion
                    dt = 0
                else:
                    matrix = np.eye(4)
                    matrix[0, 2] = matrix[1, 3] = dt
                    motion.filter.transitionMatrix = matrix
                    acceleration = np.array([[dt*dt/2, 0], [0, dt*dt/2], [dt, 0], [0, dt]])
                    motion.filter.processNoiseCov = acceleration @ acceleration.T * 0.02
                    predicted = motion.filter.predict()[:2, 0]
                    motion.residual = 0.6 * motion.residual + 0.4 * float(np.linalg.norm(predicted-point))
                    motion.filter.correct(np.asarray(point).reshape(2, 1))
                    motion.history.append((now, *point))
                motion.last_at = now
                is_bad = posture_enabled and identifier in bad
                if self.config["ergonomics"] and dt and motion.was_bad and is_bad:
                    motion.exposure.append((now-dt, now))
                motion.was_bad = is_bad
                cutoff = now-self.config["exposure_window_s"]
                while motion.exposure and motion.exposure[0][1] <= cutoff:
                    motion.exposure.popleft()
                exposure = sum(end-max(start, cutoff) for start, end in motion.exposure)
                state = motion.filter.statePost[:, 0]
                velocity = state[2:]
                ready = (len(motion.history) >= 4 and now-motion.history[0][0] >= self.config["min_history_s"]
                         and motion.residual < 0.06 and np.linalg.norm(velocity) > 0.01)
                horizon = self.config["horizon_s"]
                end = np.asarray(point) + velocity*horizon
                radius = min(0.3, 0.005 + motion.residual*(1+horizon) + horizon*0.01)
                item = {"person_id": identifier, "point": list(point), "velocity": velocity.tolist(),
                        "projection": end.tolist() if ready and self.config["trajectory"] else None,
                        "history": [[x, y] for _, x, y in motion.history], "uncertainty_radius": radius,
                        "prediction_status": "estimated" if ready else "insufficient_history",
                        "exposure_s": round(exposure, 2), "exposure_window_s": self.config["exposure_window_s"],
                        "posture_observed": is_bad, "zones": [],
                        "box_height": max((box["y2"]-box["y1"])/height, 0.01)}
                for zone in self.config["zones"] if risk_enabled else []:
                    inside = point_in_polygon(point, zone["polygon"])
                    fraction = entry_fraction(point, end, zone["polygon"]) if ready and self.config["trajectory"] else None
                    approaching = not inside and fraction is not None
                    missing = [key for key in zone["required_ppe"] if person.get("ppe", {}).get(key, {}).get("status") != "ok"]
                    zone_state = {"zone_id": zone["id"], "name": zone["name"], "inside": inside,
                                  "approaching": approaching, "eta_s": round(fraction*horizon, 2) if approaching else None,
                                  "unconfirmed_ppe": missing}
                    item["zones"].append(zone_state)
                    if (inside or approaching) and (zone["restricted"] or missing):
                        rule = "zone_entry" if inside else "predicted_zone_entry"
                        reason = "Entrada detectada" if inside else f"Entrada estimada em {fraction*horizon:.1f}s"
                        alerts.append(RuleAlert(rule=f"{rule}:{zone['id']}", feature="prevention", severity="high" if missing else "medium",
                                                message=f"{reason}: {zone['name']}", metadata={
                                                    "person_id": identifier, "person_box": box, **zone_state,
                                                    "basis": "observed_position" if inside else "constant_velocity_kalman",
                                                    "guidance": ("Verifique os EPIs exigidos antes de entrar."
                                                                 if missing else "Afaste-se da zona restrita."),
                                                }))
                if self.config["ergonomics"] and posture_enabled and exposure >= self.config["exposure_threshold_s"] and is_bad:
                    alerts.append(RuleAlert("accumulated_posture", "medium", "Exposição postural acumulada", "prevention",
                                            {"person_id": identifier, "person_box": box, "exposure_s": round(exposure, 2),
                                             "window_s": self.config["exposure_window_s"],
                                             "guidance": "Revise a posição de trabalho e a necessidade de pausa."}))
                output.append(item)
            # Lost identity is never extrapolated or charged exposure indefinitely.
            self._tracks = {key: motion for key, motion in self._tracks.items() if key in seen}
            encounters = self._encounters(output) if self.config["proximity"] else []
            for encounter in encounters:
                alerts.append(RuleAlert("converging_people", "medium", "Aproximação entre pessoas na imagem", "prevention",
                                        {"subject": "|".join(encounter["people"]), **encounter,
                                         "guidance": "Observe a circulação e mantenha passagem livre."}))
            coverage = self._coverage(frame, people, now) if self.config["coverage"] else {"status": "disabled"}
            if coverage["status"] == "degraded":
                alerts.append(RuleAlert("coverage_degraded", "high", "Cobertura visual degradada", "prevention",
                                        {"subject": "camera", **coverage, "guidance": "Verifique lente, iluminação e enquadramento."}))
            result = {"enabled": True, "units": "normalized_image", "identity_scope": "camera_session",
                      "observed_at_s": now, "observation_reused": False, "people": output,
                      "zones": deepcopy(self.config["zones"]), "encounters": encounters, "coverage": coverage,
                      "warnings": [a.message for a in alerts]}
            self._last = deepcopy(result)
            return result, alerts

    def _encounters(self, people):
        result = []
        for a, b in combinations(people, 2):
            if a["projection"] is None or b["projection"] is None:
                continue
            offset = np.asarray(a["point"])-b["point"]
            relative = np.asarray(a["velocity"])-b["velocity"]
            denominator = float(relative @ relative)
            if denominator < 1e-8 or float(offset @ relative) >= 0:
                continue
            eta = min(self.config["horizon_s"], max(0.0, -float(offset @ relative)/denominator))
            predicted_distance = float(np.linalg.norm(offset+eta*relative))
            limit = min(a["box_height"], b["box_height"])*self.config["proximity_ratio"]
            if predicted_distance < limit:
                result.append({"people": sorted([a["person_id"], b["person_id"]]), "eta_s": round(eta, 2),
                               "distance_image": round(predicted_distance, 4), "threshold_image": round(limit, 4)})
        return result

    def _coverage(self, frame, people, now):
        gray = cv2.cvtColor(cv2.resize(frame, (96, 54)), cv2.COLOR_BGR2GRAY)
        brightness, contrast = float(gray.mean()), float(gray.std())
        reasons = []
        if brightness < 20:
            reasons.append("dark_image")
        if brightness > 245:
            reasons.append("overexposed_image")
        if contrast < 4:
            reasons.append("low_contrast_possible_obstruction")
        if any((p["box"]["y2"]-p["box"]["y1"])/frame.shape[0] < 0.15 for p in people):
            reasons.append("person_too_small_for_detailed_ppe")
        candidate = tuple(reasons)
        if candidate != self._coverage_candidate:
            self._coverage_candidate, self._coverage_since = candidate, now
        degraded = bool(reasons) and now-self._coverage_since >= 1.0
        return {"status": "degraded" if degraded else "suspect" if reasons else "usable",
                "reasons": reasons, "brightness": round(brightness, 1), "contrast": round(contrast, 1),
                "method": "image_quality_heuristics", "obstruction_confirmed": False}


def draw_prevention(frame, state):
    height, width = frame.shape[:2]
    def pixel(point):
        return (int(np.clip(point[0], -1, 2)*width), int(np.clip(point[1], -1, 2)*height))
    for zone in state.get("zones", []):
        cv2.polylines(frame, [np.array([pixel(p) for p in zone["polygon"]], dtype=np.int32)], True, (0, 165, 255), 2)
        cv2.putText(frame, zone["name"], pixel(zone["polygon"][0]), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 165, 255), 1)
    for person in state.get("people", []):
        if len(person["history"]) > 1:
            cv2.polylines(frame, [np.array([pixel(p) for p in person["history"]], dtype=np.int32)], False, (220, 180, 80), 1)
        if person["projection"] is not None:
            point, end = pixel(person["point"]), pixel(person["projection"])
            cv2.arrowedLine(frame, point, end, (0, 165, 255), 2, tipLength=0.12)
            cv2.ellipse(frame, end, (int(person["uncertainty_radius"]*width), int(person["uncertainty_radius"]*height)),
                        0, 0, 360, (0, 165, 255), 1)
    return frame
