from __future__ import annotations

import json
import uuid

from flask import Blueprint, current_app, jsonify, request, send_file

from app.api.prevention import allowed_camera
from app.extensions import db
from app.models import ROLE_TECHNICAL, ScenarioRun
from app.services.prevention_service import validate_config
from app.services.scenario_service import scenario_dir
from app.utils.auth import current_user, require_role

scenarios_bp = Blueprint("scenarios", __name__)


@scenarios_bp.get("/api/cameras/<int:camera_id>/scenarios")
@require_role(ROLE_TECHNICAL)
def list_scenarios(camera_id):
    _, error = allowed_camera(camera_id)
    if error:
        return error
    runs = ScenarioRun.query.filter_by(camera_id=camera_id).order_by(ScenarioRun.created_at.desc()).limit(20).all()
    return jsonify({"runs": [{"id": run.id, "camera_id": run.camera_id, "status": run.status, "error": run.error,
                              "result": None, "created_at": run.created_at.isoformat()} for run in runs]})


@scenarios_bp.post("/api/cameras/<int:camera_id>/scenarios")
@require_role(ROLE_TECHNICAL)
def create_scenario(camera_id):
    camera, error = allowed_camera(camera_id)
    if error:
        return error
    video = request.files.get("video")
    if video is None or not video.filename or not video.filename.lower().endswith(".mp4"):
        return jsonify({"error": "Envie um vídeo MP4 local (até 128 MB)."}), 400
    try:
        raw = json.loads(request.form.get("configs", "[]"))
        if not isinstance(raw, list) or len(raw) > 2:
            raise ValueError("Informe uma ou duas configurações para comparar.")
        configs = [validate_config(config) for config in raw] or [validate_config(camera.prevention_json or {})]
        if not all(config["enabled"] for config in configs):
            raise ValueError("Ative a prevenção nas configurações do laboratório.")
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400
    service = current_app.extensions["scenario_service"]
    if not service.reserve():
        return jsonify({"error": "Já existe um cenário em processamento. Aguarde sua conclusão."}), 409
    identifier = uuid.uuid4().hex
    directory = scenario_dir(current_app)/identifier
    started = False
    try:
        directory.mkdir(parents=True, exist_ok=False)
        total = 0
        with (directory/"source.mp4").open("wb") as output:
            while chunk := video.stream.read(64*1024):
                total += len(chunk)
                if total > 128*1024*1024:
                    return jsonify({"error": "Vídeo supera o limite de 128 MB."}), 413
                output.write(chunk)
        if not total:
            return jsonify({"error": "Vídeo vazio."}), 400
        user = current_user()
        run = ScenarioRun(id=identifier, camera_id=camera_id, owner_id=user.id if user else None)
        db.session.add(run)
        db.session.commit()
        service.start(identifier, configs)
        started = True
        return jsonify(run.to_dict()), 202
    finally:
        if not started:
            (directory/"source.mp4").unlink(missing_ok=True)
            service.release()


@scenarios_bp.get("/api/scenarios/<run_id>")
@require_role(ROLE_TECHNICAL)
def get_scenario(run_id):
    run = db.session.get(ScenarioRun, run_id)
    if not run:
        return jsonify({"error": "Cenário não encontrado."}), 404
    _, error = allowed_camera(run.camera_id)
    if error:
        return error
    return jsonify(run.to_dict() | {"progress": current_app.extensions["scenario_service"].progress(run_id)})


@scenarios_bp.get("/api/scenarios/<run_id>/images/<filename>")
@require_role(ROLE_TECHNICAL)
def scenario_image(run_id, filename):
    run = db.session.get(ScenarioRun, run_id)
    if not run:
        return jsonify({"error": "Cenário não encontrado."}), 404
    _, error = allowed_camera(run.camera_id)
    if error:
        return error
    allowed = {sample["image"] for sample in (run.result_json or {}).get("samples", [])}
    if filename not in allowed:
        return jsonify({"error": "Imagem não encontrada."}), 404
    response = send_file(scenario_dir(current_app)/run_id/filename, mimetype="image/jpeg")
    response.headers["Cache-Control"] = "private, no-store"
    return response
