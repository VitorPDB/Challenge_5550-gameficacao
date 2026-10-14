from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from app.extensions import db
from app.models import ROLE_TECHNICAL, Camera
from app.utils.auth import _operador_sem_setor, camera_permitida, camera_scope, erro_fora_do_escopo, login_required, require_role

features_bp = Blueprint("features", __name__)


def _manager_da_camera(camera_id: int | None):
    """FeatureManager de quem manda na câmera. Sem `camera_id`, o global (a
    câmera padrão), como sempre foi."""
    if camera_id is None:
        return current_app.extensions["feature_manager"]
    return current_app.extensions["monitor_service"].features_of(camera_id)


@features_bp.get("/features")
@login_required
def get_features():
    if _operador_sem_setor():
        return erro_fora_do_escopo()
    camera_id = request.args.get("camera_id", type=int)
    if camera_id is None:
        camera_id = camera_scope()
    if camera_id is not None and not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    manager = _manager_da_camera(camera_id)
    return jsonify({"features": [item.to_dict() for item in manager.list()], "camera_id": camera_id})


@features_bp.put("/features")
@features_bp.patch("/features")
@require_role(ROLE_TECHNICAL)
def update_features():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Payload inválido. Use {'features': {'helmet': true}}"}), 400
    camera_id = request.args.get("camera_id", type=int)
    if camera_id is None and isinstance(payload.get("camera_id"), int):
        camera_id = payload["camera_id"]
    updates = payload.get("features", payload)
    if not isinstance(updates, dict):
        return jsonify({"error": "Payload inválido. Use {'features': {'helmet': true}}"}), 400
    updates = {str(key): bool(value) for key, value in updates.items() if key != "camera_id"}

    manager = _manager_da_camera(camera_id)
    updated = manager.update(updates)

    monitor = current_app.extensions["monitor_service"]
    persisted_id = camera_id if camera_id is not None else getattr(monitor, "_default_camera_id", None)
    if persisted_id is not None:
        # Persiste no banco pra não voltar ao estado antigo no próximo boot.
        camera = db.session.get(Camera, persisted_id)
        if camera is not None:
            atual = dict(camera.features_json or {})
            atual.update(updates)
            camera.features_json = atual
            db.session.commit()
            monitor.load_cameras_from_db()

    response = {"features": [item.to_dict() for item in updated], "camera_id": camera_id}
    current_app.extensions["monitor_service"].socketio.emit("features_updated", response)
    return jsonify(response)
