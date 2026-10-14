from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.api.evidence import serve_alert_evidence
from app.models import ROLE_TECHNICAL
from app.repositories.alert_repository import AlertRepository
from app.repositories.event_repository import EventRepository
from app.utils.auth import _operador_sem_setor, camera_scope, escopo_ou_erro, login_required, require_role

runtime_bp = Blueprint("runtime", __name__)


@runtime_bp.get("/model")
@login_required
def model_diagnostics():
    camera_id, erro = escopo_ou_erro()
    if erro:
        return erro
    monitor = current_app.extensions["monitor_service"]
    return jsonify(monitor.status(camera_id=camera_id).get("model", {}))


@runtime_bp.get("/settings")
@login_required
def get_settings():
    camera_id, erro = escopo_ou_erro()
    if erro:
        return erro
    monitor = current_app.extensions["monitor_service"]
    return jsonify(
        {"settings": monitor.settings(camera_id=camera_id), "overlay": monitor.get_overlay(camera_id=camera_id)}
    )


@runtime_bp.patch("/settings")
@require_role(ROLE_TECHNICAL)
def patch_settings():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload inválido"}), 400
    monitor = current_app.extensions["monitor_service"]
    return jsonify({"settings": monitor.update_settings(payload)})


@runtime_bp.get("/overlay")
@login_required
def get_overlay():
    camera_id, erro = escopo_ou_erro()
    if erro:
        return erro
    monitor = current_app.extensions["monitor_service"]
    return jsonify({"overlay": monitor.get_overlay(camera_id=camera_id)})


@runtime_bp.patch("/overlay")
@require_role(ROLE_TECHNICAL)
def patch_overlay():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload inválido"}), 400
    monitor = current_app.extensions["monitor_service"]
    return jsonify({"overlay": monitor.update_overlay(payload)})


@runtime_bp.get("/risk-area")
@login_required
def get_risk_area():
    camera_id, erro = escopo_ou_erro()
    if erro:
        return erro
    monitor = current_app.extensions["monitor_service"]
    if not hasattr(monitor, "risk_area_state"):
        return jsonify({"error": "monitor atual não suporta área de risco runtime"}), 501
    return jsonify({"risk_area": monitor.risk_area_state(camera_id=camera_id)})


@runtime_bp.patch("/risk-area")
@require_role(ROLE_TECHNICAL)
def patch_risk_area():
    camera_id, erro = escopo_ou_erro()
    if erro:
        return erro
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload inválido"}), 400
    monitor = current_app.extensions["monitor_service"]
    if not hasattr(monitor, "update_risk_area"):
        return jsonify({"error": "monitor atual não suporta área de risco runtime"}), 501
    try:
        return jsonify({"risk_area": monitor.update_risk_area(payload, camera_id=camera_id)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@runtime_bp.get("/events")
@login_required
def list_events():
    try:
        limit = min(int(request.args.get("limit", 80)), 300)
    except ValueError:
        limit = 80
    event_type = request.args.get("event_type")
    severity = request.args.get("severity")
    if _operador_sem_setor():
        return jsonify({"items": [], "count": 0})

    escopo = camera_scope()
    events = EventRepository().list_recent(
        limit=limit,
        event_type=event_type,
        severity=severity,
        camera_id=escopo if escopo is not None else request.args.get("camera_id", type=int),
    )
    return jsonify({"items": [item.to_dict() for item in events], "count": len(events)})


@runtime_bp.get("/snapshots/<path:filename>")
@login_required
def get_snapshot(filename: str):
    alert = AlertRepository().get_by_frame_ref(f"/snapshots/{filename}")
    if alert is None:
        return jsonify({"error": "evidência não encontrada"}), 404
    return serve_alert_evidence(alert)
