from __future__ import annotations

from pathlib import Path

from flask import current_app, jsonify, send_from_directory

from app.config import BASE_DIR
from app.utils.auth import camera_permitida, erro_fora_do_escopo


def serve_alert_evidence(alert):
    """Autoriza a câmera antes de servir uma evidência, mesmo sem worker ativo."""
    if not camera_permitida(alert.camera_id):
        return erro_fora_do_escopo()
    reference = str(alert.frame_ref or "")
    if not reference.startswith("/snapshots/"):
        return jsonify({"error": "alerta sem evidência válida vinculada"}), 404
    filename = reference.removeprefix("/snapshots/")
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        return jsonify({"error": "referência de evidência inválida"}), 404

    directory = Path(current_app.config.get("SNAPSHOT_DIR", "runtime/snapshots"))
    if not directory.is_absolute():
        directory = Path(BASE_DIR) / directory
    response = send_from_directory(directory, filename, as_attachment=False)
    response.headers["Cache-Control"] = "private, no-store"
    return response
