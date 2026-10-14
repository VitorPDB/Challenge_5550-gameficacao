from io import BytesIO

from app.extensions import db
from app.models import Alert, Camera, PreventionMission


def camera():
    item = Camera(name="Setor A", source_type="Arquivo", source="local.mp4")
    db.session.add(item)
    db.session.commit()
    return item.id


def test_configuration_persists_and_does_not_leak_between_workers(real_monitor_client, real_monitor_app):
    first, second = camera(), camera()
    monitor = real_monitor_app.extensions["monitor_service"]
    monitor.load_cameras_from_db()
    response = real_monitor_client.put(f"/api/cameras/{first}/prevention", json={"horizon_s": 3.5})
    assert response.status_code == 200
    assert monitor._workers[first].prevention_service.config["horizon_s"] == 3.5
    assert monitor._workers[second].prevention_service.config["horizon_s"] == 2
    assert db.session.get(Camera, first).prevention_json["horizon_s"] == 3.5
    assert real_monitor_client.put(f"/api/cameras/{first}/prevention", json={"horizon_s": -1}).status_code == 400


def test_missions_require_evidence_and_verified_status_before_points(client, app):
    cam = camera()
    response = client.post(f"/api/cameras/{cam}/missions",
                           json={"team": "Turno A", "title": "Revisar circulação", "category": "prevention"})
    assert response.status_code == 201
    identifier = response.get_json()["id"]
    assert client.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 409
    assert client.post(f"/api/missions/{identifier}/submit", json={"evidence": ""}).status_code == 400
    assert client.post(f"/api/missions/{identifier}/submit", json={"evidence": "Orientação realizada com a equipe."}).status_code == 200
    assert client.get(f"/api/cameras/{cam}/missions").get_json()["teams"][0]["points"] == 0
    assert client.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 200
    assert client.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 409
    result = client.get(f"/api/cameras/{cam}/missions").get_json()
    assert result["teams"][0]["points"] == 100
    assert result["teams"][0]["achievements"] == ["Primeira ação preventiva"]
    db.session.expire_all()
    assert db.session.get(PreventionMission, identifier).status == "verified"


def test_recurrence_suggestions_exclude_false_positives(client, app):
    cam = camera()
    db.session.add(Alert(camera_id=cam, rule="ppe", feature="helmet", severity="high", message="Capacete"))
    db.session.add(Alert(camera_id=cam, rule="ppe", feature="glasses", severity="high", message="Óculos",
                         metadata_json={"false_positive": True}))
    db.session.commit()
    suggestions = client.get(f"/api/cameras/{cam}/missions").get_json()["suggestions"]
    assert [s["category"] for s in suggestions] == ["helmet"]


def test_team_names_reuse_identity_and_pending_duplicates_are_rejected(client, app):
    cam = camera()
    mission = {"team": "Turno A", "category": "prevention", "title": "Revisar circulação"}
    assert client.post(f"/api/cameras/{cam}/missions", json=mission).status_code == 201
    duplicate = {**mission, "team": " turno  a "}
    assert client.post(f"/api/cameras/{cam}/missions", json=duplicate).status_code == 409
    second = client.post(f"/api/cameras/{cam}/missions", json={**duplicate, "title": "Orientar equipe"})
    assert second.status_code == 201
    assert second.get_json()["team"] == "Turno A"
    result = client.get(f"/api/cameras/{cam}/missions").get_json()
    assert len(result["teams"]) == 1
    assert result["teams"][0]["open"] == 2
    assert client.post(f"/api/cameras/{cam}/missions", json={**mission, "category": "categoria-inventada"}).status_code == 400


def test_lab_rejects_empty_file_and_serializes_jobs(client, app, monkeypatch, tmp_path):
    cam = camera()
    app.config["SCENARIO_DIR"] = str(tmp_path)
    service = app.extensions["scenario_service"]
    assert client.post(f"/api/cameras/{cam}/scenarios", data={"video": (BytesIO(b""), "empty.mp4")}).status_code == 400
    assert service.reserve()
    response = client.post(f"/api/cameras/{cam}/scenarios", data={"video": (BytesIO(b"a"), "clip.mp4")})
    assert response.status_code == 409
    service.release()
    monkeypatch.setattr(service, "start", lambda *_args: None)
    response = client.post(f"/api/cameras/{cam}/scenarios", data={"video": (BytesIO(b"a"), "clip.mp4")})
    assert response.status_code == 202
    identifier = response.get_json()["id"]
    assert client.get(f"/api/scenarios/{identifier}").get_json()["status"] == "queued"
    assert client.get(f"/api/scenarios/{identifier}/images/source.mp4").status_code == 404
    service.release()
