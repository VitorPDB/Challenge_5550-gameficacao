import pytest
from app import create_app
from app.config import AuthTestConfig
from app.extensions import db
from app.models import Camera, ScenarioRun
from app.services.auth_service import AuthService


@pytest.fixture
def secured(tmp_path):
    class Config(AuthTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path/'prevention.db'}"
    app = create_app(Config)
    with app.app_context():
        db.create_all()
        cameras = [Camera(name="A", source="0"), Camera(name="B", source="1")]
        db.session.add_all(cameras)
        db.session.commit()
        for role, camera in (("operator", cameras[0].id), ("technical", None), ("supervisor", None)):
            AuthService().create_user(email=f"{role}@test.com", name=role, password="senha-forte-de-teste", role=role, camera_id=camera)
        db.session.add(ScenarioRun(id="b"*32, camera_id=cameras[1].id))
        db.session.commit()
    yield app.test_client()


def login(client, role):
    response = client.post("/api/auth/login", json={"email": f"{role}@test.com", "password": "senha-forte-de-teste"})
    assert response.status_code == 200


def test_operator_scope_and_lab_role_enforced(secured):
    assert secured.get("/api/cameras/1/prevention").status_code == 401
    login(secured, "operator")
    assert secured.get("/api/cameras/1/prevention").status_code == 200
    assert secured.get("/api/cameras/2/prevention").status_code == 404
    assert secured.get("/api/cameras/2/missions").status_code == 404
    assert secured.put("/api/cameras/1/prevention", json={}).status_code == 403
    assert secured.get(f"/api/scenarios/{'b'*32}").status_code == 403
    assert secured.post("/api/cameras/1/scenarios").status_code == 403


def test_mission_review_requires_supervisor_and_another_submitter(secured):
    login(secured, "technical")
    response = secured.post("/api/cameras/1/missions", json={"team": "A", "category": "prevention", "title": "Revisar circulação"})
    identifier = response.get_json()["id"]
    secured.post(f"/api/missions/{identifier}/submit", json={"evidence": "A equipe revisou a circulação."})
    assert secured.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 403
    login(secured, "supervisor")
    assert secured.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 200
    response = secured.post("/api/cameras/1/missions", json={"team": "A", "category": "posture", "title": "Revisar postura"})
    identifier = response.get_json()["id"]
    secured.post(f"/api/missions/{identifier}/submit", json={"evidence": "A equipe revisou a posição de trabalho."})
    assert secured.post(f"/api/missions/{identifier}/review", json={"approve": True}).status_code == 403
