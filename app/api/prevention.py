from __future__ import annotations

from collections import Counter
from datetime import timedelta
from unicodedata import normalize

from flask import Blueprint, current_app, jsonify, request

from app.extensions import db
from app.models import ROLE_SUPERVISOR, ROLE_TECHNICAL, Alert, Camera, PreventionMission, utc_now
from app.services.gamification_service import CATEGORIES, build_team_progress
from app.services.prevention_service import validate_config
from app.utils.auth import camera_permitida, current_user, erro_fora_do_escopo, login_required, require_role

prevention_bp = Blueprint("prevention", __name__)


def allowed_camera(camera_id):
    if not camera_permitida(camera_id):
        return None, erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    return (camera, None) if camera else (None, (jsonify({"error": "Câmera não encontrada."}), 404))


@prevention_bp.get("/api/cameras/<int:camera_id>/prevention")
@login_required
def get_prevention(camera_id):
    camera, error = allowed_camera(camera_id)
    if error:
        return error
    monitor = current_app.extensions["monitor_service"]
    try:
        analysis = monitor.latest_analysis(camera_id=camera_id) or {}
    except LookupError:
        analysis = {}
    return jsonify({"camera_id": camera_id, "config": validate_config(camera.prevention_json or {}),
                    "state": analysis.get("prevention"), "status": monitor.status(camera_id=camera_id) if analysis else None})


@prevention_bp.put("/api/cameras/<int:camera_id>/prevention")
@require_role(ROLE_TECHNICAL)
def put_prevention(camera_id):
    camera, error = allowed_camera(camera_id)
    if error:
        return error
    try:
        config = validate_config(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    camera.prevention_json = config
    db.session.commit()
    monitor = current_app.extensions["monitor_service"]
    if hasattr(monitor, "load_cameras_from_db"):
        monitor.load_cameras_from_db()
    return jsonify({"camera_id": camera_id, "config": config})


EDUCATION = {
    "helmet": "Orientação sobre ajuste e uso do capacete",
    "glasses": "Orientação sobre proteção ocular e conservação",
    "ear_protection": "Orientação sobre colocação do protetor auricular",
    "posture": "Revisão da posição de trabalho e pausas",
    "prevention": "Revisão da circulação e dos requisitos das zonas",
    "coverage": "Verificação da iluminação, lente e enquadramento",
    "circulation": "Orientação sobre circulação e aproximação de zonas",
}


@prevention_bp.get("/api/cameras/<int:camera_id>/missions")
@login_required
def missions(camera_id):
    _, error = allowed_camera(camera_id)
    if error:
        return error
    alerts = Alert.query.filter(Alert.camera_id == camera_id, Alert.created_at >= utc_now()-timedelta(days=7))
    alerts = alerts.order_by(Alert.created_at.desc()).limit(500).all()
    counts = Counter()
    for alert in alerts:
        if (alert.metadata_json or {}).get("false_positive"):
            continue
        category = alert.feature or "general"
        if alert.rule == "coverage_degraded":
            category = "coverage"
        elif alert.rule == "accumulated_posture":
            category = "posture"
        elif alert.rule.startswith(("predicted_zone_entry:", "zone_entry:", "converging_people")):
            category = "circulation"
        if category not in CATEGORIES:
            category = "prevention"
        counts[category] += 1
    missions = PreventionMission.query.filter_by(camera_id=camera_id).order_by(PreventionMission.id.desc()).limit(200).all()
    # Progress must survive pagination and process restarts.
    all_missions = PreventionMission.query.filter_by(camera_id=camera_id).all()
    teams = build_team_progress(all_missions, utc_now())
    return jsonify({"missions": [m.to_dict() for m in missions], "teams": teams,
                    "categories": [{"key": key, "label": label} for key, label in CATEGORIES.items()],
                    "suggestions": [{"category": category, "occurrences": count,
                                     "title": EDUCATION.get(category, f"Orientação e revisão: {category}")}
                                    for category, count in counts.most_common(6)],
                    "basis": "Sugestões: até 500 ocorrências dos últimos 7 dias, excluindo falsos positivos. "
                    "100 XP por ação verificada; metas não dão pontos extras. Evolução por equipe e câmera, não avaliação individual."})


@prevention_bp.post("/api/cameras/<int:camera_id>/missions")
@require_role(ROLE_TECHNICAL)
def create_mission(camera_id):
    _, error = allowed_camera(camera_id)
    if error:
        return error
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or set(body) != {"team", "category", "title"} or any(
        not isinstance(body[k], str) or not body[k].strip() or len(body[k]) > limit
        for k, limit in (("team", 120), ("category", 80), ("title", 200))
    ):
        return jsonify({"error": "Informe equipe, categoria e título válidos."}), 400
    if body["category"].strip() not in CATEGORIES:
        return jsonify({"error": "Escolha uma categoria de prevenção válida."}), 400
    team = " ".join(normalize("NFKC", body["team"]).split())
    names = db.session.query(PreventionMission.team).filter_by(camera_id=camera_id).distinct().all()
    for (existing,) in names:
        if " ".join(normalize("NFKC", existing).split()).casefold() == team.casefold():
            team = existing
            break
    pending = PreventionMission.query.filter(PreventionMission.camera_id == camera_id, PreventionMission.team == team,
                                            PreventionMission.category == body["category"].strip(),
                                            PreventionMission.status.in_(["open", "submitted"])).all()
    if any(m.title.casefold() == body["title"].strip().casefold() for m in pending):
        return jsonify({"error": "Esta equipe já tem uma missão igual em andamento."}), 409
    user = current_user()
    mission = PreventionMission(camera_id=camera_id, created_by=user.id if user else None,
                                team=team, category=body["category"].strip(), title=body["title"].strip())
    db.session.add(mission)
    db.session.commit()
    return jsonify(mission.to_dict()), 201


@prevention_bp.post("/api/missions/<int:mission_id>/submit")
@login_required
def submit_mission(mission_id):
    mission = db.session.get(PreventionMission, mission_id)
    if not mission:
        return jsonify({"error": "Missão não encontrada."}), 404
    if not camera_permitida(mission.camera_id):
        return erro_fora_do_escopo()
    body = request.get_json(silent=True)
    evidence = body.get("evidence") if isinstance(body, dict) else None
    if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 1000:
        return jsonify({"error": "Descreva a ação realizada (10 a 1000 caracteres)."}), 400
    user = current_user()
    changed = PreventionMission.query.filter_by(id=mission_id, status="open").update(
        {"status": "submitted", "evidence": evidence.strip(), "submitted_by": user.id if user else None})
    if not changed:
        db.session.rollback()
        return jsonify({"error": "Missão já enviada ou verificada."}), 409
    db.session.commit()
    return jsonify(db.session.get(PreventionMission, mission_id).to_dict())


@prevention_bp.post("/api/missions/<int:mission_id>/review")
@require_role(ROLE_SUPERVISOR)
def review_mission(mission_id):
    mission = db.session.get(PreventionMission, mission_id)
    if not mission:
        return jsonify({"error": "Missão não encontrada."}), 404
    if not camera_permitida(mission.camera_id):
        return erro_fora_do_escopo()
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get("approve")) is not bool:
        return jsonify({"error": "Informe approve: true/false."}), 400
    user = current_user()
    if user and user.id == mission.submitted_by:
        return jsonify({"error": "A revisão deve ser feita por outra pessoa."}), 403
    changed = PreventionMission.query.filter_by(id=mission_id, status="submitted").update(
        {"status": "verified" if body["approve"] else "open", "verified_by": user.id if user else None,
         "verified_at": utc_now() if body["approve"] else None})
    if not changed:
        db.session.rollback()
        return jsonify({"error": "A missão precisa estar aguardando revisão."}), 409
    db.session.commit()
    return jsonify(db.session.get(PreventionMission, mission_id).to_dict())
