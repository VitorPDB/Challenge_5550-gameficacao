"""A zona de risco é POR CÂMERA: editar uma não pode mexer nas outras, e a
edição sobrevive a reiniciar o worker (antes vivia só em memória)."""
from __future__ import annotations

from app.models import Camera

ZONA_A = [{"x": 0.1, "y": 0.1}, {"x": 0.4, "y": 0.1}, {"x": 0.4, "y": 0.9}]
ZONA_B = [{"x": 0.6, "y": 0.2}, {"x": 0.9, "y": 0.2}, {"x": 0.9, "y": 0.8}, {"x": 0.6, "y": 0.8}]


def _create(client, name):
    r = client.post("/api/cameras", json={"name": name, "source_type": "USB", "source": "0"})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


def _polygon(client, cam_id):
    return client.get(f"/api/cameras/{cam_id}/risk-area").get_json()["risk_area"]["polygon"]


def test_editar_uma_camera_nao_mexe_na_outra(real_monitor_client):
    a = _create(real_monitor_client, "A")
    b = _create(real_monitor_client, "B")
    padrao_b = _polygon(real_monitor_client, b)

    r = real_monitor_client.put(f"/api/cameras/{a}/risk-area", json={"polygon": ZONA_A})
    assert r.status_code == 200, r.get_json()

    assert _polygon(real_monitor_client, a) == [{"x": p["x"], "y": p["y"]} for p in ZONA_A]
    assert _polygon(real_monitor_client, b) == padrao_b  # intocada

    real_monitor_client.put(f"/api/cameras/{b}/risk-area", json={"polygon": ZONA_B})
    assert len(_polygon(real_monitor_client, a)) == 3
    assert len(_polygon(real_monitor_client, b)) == 4


def test_zona_e_persistida_no_banco(real_monitor_app, real_monitor_client):
    a = _create(real_monitor_client, "A")
    real_monitor_client.put(f"/api/cameras/{a}/risk-area", json={"polygon": ZONA_A})

    with real_monitor_app.app_context():
        from app.extensions import db

        assert db.session.get(Camera, a).risk_polygon == [[0.1, 0.1], [0.4, 0.1], [0.4, 0.9]]


def test_worker_novo_nasce_com_a_zona_salva(real_monitor_app, real_monitor_client):
    """Reiniciar o servidor (ou o CRUD reconstruir o worker) mantém a zona."""
    a = _create(real_monitor_client, "A")
    real_monitor_client.put(f"/api/cameras/{a}/risk-area", json={"polygon": ZONA_B})

    monitor = real_monitor_app.extensions["monitor_service"]
    with real_monitor_app.app_context():
        from app.extensions import db

        camera = db.session.get(Camera, a)
    novo = monitor._build_worker(camera)
    assert novo.risk_area_state()["polygon"] == [{"x": p["x"], "y": p["y"]} for p in ZONA_B]


def test_poligono_invalido_e_recusado(real_monitor_client):
    a = _create(real_monitor_client, "A")
    r = real_monitor_client.put(f"/api/cameras/{a}/risk-area", json={"polygon": [{"x": 0.1, "y": 0.1}]})
    assert r.status_code == 400
    r = real_monitor_client.put(f"/api/cameras/{a}/risk-area", json={"polygon": [{"x": "a", "y": 0}] * 3})
    assert r.status_code == 400


def test_camera_inexistente_da_erro_nao_500(real_monitor_client):
    r = real_monitor_client.get("/api/cameras/999/risk-area")
    assert r.status_code in (404, 409)
