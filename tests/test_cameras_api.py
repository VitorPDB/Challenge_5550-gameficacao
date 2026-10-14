"""CRUD e controle de câmeras contra o MonitorService REAL (sem DummyMonitor).

Estas rotas nunca tiveram cobertura: o conftest trocava o monitor inteiro por
um stub, então `MonitorService`, `CameraWorker` e todo o `/api/cameras/*`
passavam batido na suíte.
"""
from __future__ import annotations

from app.models import DEFAULT_CAMERA_FEATURES, Camera


def _create(client, **overrides):
    payload = {"name": "Portaria", "source_type": "USB", "source": "0", **overrides}
    return client.post("/api/cameras", json=payload)


# ---------------------------------------------------------------- CRUD ------
def test_lista_comeca_vazia(real_monitor_client):
    response = real_monitor_client.get("/api/cameras")
    assert response.status_code == 200
    assert response.get_json() == {"items": [], "count": 0}


def test_cria_camera_com_defaults(real_monitor_client):
    response = _create(real_monitor_client)
    assert response.status_code == 201
    body = response.get_json()
    assert body["name"] == "Portaria"
    assert body["source"] == "0"
    assert body["enabled"] is True
    assert body["fps"] == 12
    # Câmera nova nasce com TODAS as features ligadas (ver DEFAULT_CAMERA_FEATURES).
    assert body["features"] == DEFAULT_CAMERA_FEATURES


def test_features_novas_aparecem_em_camera_antiga(real_monitor_app, real_monitor_client):
    """Câmera gravada antes de uma feature existir não pode reportá-la como
    desligada — o worker usa o default, e a UI precisa dizer o mesmo."""
    with real_monitor_app.app_context():
        from app.extensions import db

        camera = Camera(name="Antiga", source_type="USB", source="0")
        # Simula o JSON de antes de glasses/mask/safety_shoe existirem.
        camera.features_json = {
            "helmet": True,
            "vest": True,
            "gloves": False,
            "pose": True,
            "falls": True,
            "posture": True,
            "risk_area": True,
        }
        db.session.add(camera)
        db.session.commit()

    features = real_monitor_client.get("/api/cameras").get_json()["items"][0]["features"]
    assert features["gloves"] is False       # escolha explícita do usuário preservada
    assert features["glasses"] is True       # chave nova cai no default, não em undefined
    assert features["mask"] is True
    assert features["safety_shoe"] is True


def test_get_camera_inexistente(real_monitor_client):
    assert real_monitor_client.get("/api/cameras/999").status_code == 404


def test_atualiza_camera(real_monitor_client):
    camera_id = _create(real_monitor_client).get_json()["id"]
    response = real_monitor_client.put(f"/api/cameras/{camera_id}", json={"name": "Setor B", "fps": 20})
    assert response.status_code == 200
    body = response.get_json()
    assert body["name"] == "Setor B"
    assert body["fps"] == 20


def test_remove_camera(real_monitor_client):
    camera_id = _create(real_monitor_client).get_json()["id"]
    assert real_monitor_client.delete(f"/api/cameras/{camera_id}").status_code == 200
    assert real_monitor_client.get(f"/api/cameras/{camera_id}").status_code == 404
    assert real_monitor_client.get("/api/cameras").get_json()["count"] == 0


# ---------------------------------------------------------- validação -------
def test_nome_obrigatorio(real_monitor_client):
    response = real_monitor_client.post("/api/cameras", json={"source_type": "USB", "source": "0"})
    assert response.status_code == 400
    assert "name" in response.get_json()["error"]


def test_source_obrigatorio(real_monitor_client):
    response = real_monitor_client.post("/api/cameras", json={"name": "X", "source_type": "USB"})
    assert response.status_code == 400
    assert "source" in response.get_json()["error"]


def test_source_type_invalido(real_monitor_client):
    response = _create(real_monitor_client, source_type="Webcam")
    assert response.status_code == 400
    assert "source_type" in response.get_json()["error"]


def test_feature_desconhecida_e_rejeitada(real_monitor_client):
    response = _create(real_monitor_client, features={"capacete": True})
    assert response.status_code == 400
    assert "capacete" in response.get_json()["error"]


def test_fps_e_resolucao_sao_limitados(real_monitor_client):
    body = _create(real_monitor_client, fps=999, width=99999, height=1).get_json()
    assert body["fps"] == 60          # teto
    assert body["width"] == 3840      # teto
    assert body["height"] == 120      # piso


# ------------------------------------------------- sincronia dos workers ----
def test_crud_sincroniza_workers_do_monitor(real_monitor_app, real_monitor_client):
    """O CRUD tem que refletir no motor de captura sem reiniciar o servidor."""
    monitor = real_monitor_app.extensions["monitor_service"]
    assert monitor._workers == {}

    camera_id = _create(real_monitor_client).get_json()["id"]
    assert camera_id in monitor._workers
    assert monitor._default_camera_id == camera_id

    # Desabilitar remove o worker; reabilitar traz de volta.
    real_monitor_client.put(f"/api/cameras/{camera_id}", json={"enabled": False})
    assert camera_id not in monitor._workers

    real_monitor_client.put(f"/api/cameras/{camera_id}", json={"enabled": True})
    assert camera_id in monitor._workers

    real_monitor_client.delete(f"/api/cameras/{camera_id}")
    assert monitor._workers == {}


def test_editar_fonte_recria_o_worker(real_monitor_app, real_monitor_client):
    monitor = real_monitor_app.extensions["monitor_service"]
    camera_id = _create(real_monitor_client).get_json()["id"]
    worker_antigo = monitor._workers[camera_id]

    real_monitor_client.put(f"/api/cameras/{camera_id}", json={"source": "1"})
    worker_novo = monitor._workers[camera_id]

    assert worker_novo is not worker_antigo
    assert str(worker_novo.video_stream.source) == "1"


def test_editar_so_o_nome_nao_recria_o_worker(real_monitor_app, real_monitor_client):
    """Renomear não pode derrubar a captura — só mudança de fonte/fps/resolução
    justifica reconstruir o worker."""
    monitor = real_monitor_app.extensions["monitor_service"]
    camera_id = _create(real_monitor_client).get_json()["id"]
    worker_antigo = monitor._workers[camera_id]

    real_monitor_client.put(f"/api/cameras/{camera_id}", json={"name": "Outro nome"})
    assert monitor._workers[camera_id] is worker_antigo


# --------------------------------------------------- rotas por câmera -------
def test_status_por_camera(real_monitor_client):
    camera_id = _create(real_monitor_client).get_json()["id"]
    body = real_monitor_client.get(f"/api/cameras/{camera_id}/status").get_json()
    assert body["running"] is False
    assert body["camera_id"] == camera_id


def test_rotas_por_camera_404_quando_nao_existe(real_monitor_client):
    for path in ("status", "analysis", "video_feed"):
        assert real_monitor_client.get(f"/api/cameras/999/{path}").status_code == 404
    for path in ("start", "stop"):
        assert real_monitor_client.post(f"/api/cameras/999/{path}").status_code == 404


def test_camera_desabilitada_responde_409_sem_worker(real_monitor_client):
    camera_id = _create(real_monitor_client).get_json()["id"]
    real_monitor_client.put(f"/api/cameras/{camera_id}", json={"enabled": False})

    response = real_monitor_client.get(f"/api/cameras/{camera_id}/status")
    assert response.status_code == 409
    assert response.get_json()["running"] is False


def test_analysis_por_camera_vazio_antes_de_iniciar(real_monitor_client):
    camera_id = _create(real_monitor_client).get_json()["id"]
    response = real_monitor_client.get(f"/api/cameras/{camera_id}/analysis")
    assert response.status_code == 200
    assert response.get_json() == {}


def test_video_feed_por_camera_serve_placeholder(real_monitor_client):
    """Sem monitoramento rodando, o MJPEG serve frame de placeholder em vez de
    quebrar a conexão."""
    camera_id = _create(real_monitor_client).get_json()["id"]
    response = real_monitor_client.get(f"/api/cameras/{camera_id}/video_feed")
    assert response.status_code == 200
    primeiro_chunk = next(iter(response.response))
    assert b"\xff\xd8" in primeiro_chunk  # magic number de JPEG
    response.response.close()


def test_frame_avulso_serve_um_jpeg_e_encerra(real_monitor_client):
    """A miniatura do painel pede um quadro por vez (conexão curta), pra não
    prender as conexões do navegador como o MJPEG contínuo fazia."""
    camera_id = _create(real_monitor_client).get_json()["id"]
    response = real_monitor_client.get(f"/api/cameras/{camera_id}/frame.jpg")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data.startswith(b"\xff\xd8")
    assert response.headers["Cache-Control"] == "no-store"


# ------------------------------------------------------ rotas legadas -------
def test_video_feed_legado_sem_camera_nao_explode(real_monitor_client):
    """Nenhuma câmera cadastrada é estado VÁLIDO (o seed automático foi
    removido). A rota legada precisa servir placeholder — antes o
    CameraNotFoundError subia de dentro do generator e derrubava a conexão."""
    response = real_monitor_client.get("/video_feed")
    assert response.status_code == 200
    primeiro_chunk = next(iter(response.response))
    assert b"\xff\xd8" in primeiro_chunk
    response.response.close()


def test_rotas_legadas_respondem_404_sem_camera(real_monitor_client):
    """Sem câmera cadastrada, as rotas de "câmera padrão" devolvem 404 com
    dica — pelo errorhandler de CameraNotFoundError em app/__init__.py."""
    response = real_monitor_client.get("/status")
    assert response.status_code == 404
    assert "hint" in response.get_json()


def test_cinco_cameras_ip_tem_todas_as_regras_e_estado_independente(real_monitor_app, real_monitor_client):
    monitor = real_monitor_app.extensions["monitor_service"]
    ids = [
        _create(real_monitor_client, name=f"Setor {i}", source_type="RTSP", source=f"rtsp://192.0.2.{i}/stream")
        .get_json()["id"]
        for i in range(1, 6)
    ]
    workers = [monitor._workers[camera_id] for camera_id in ids]
    assert len({id(worker.feature_manager) for worker in workers}) == 5
    assert len({id(worker.person_tracker) for worker in workers}) == 5
    assert len({id(worker.detector) for worker in workers}) == 1
    assert all(all(worker.feature_manager.is_enabled(key) for key in DEFAULT_CAMERA_FEATURES) for worker in workers)
    original = workers[3]
    response = real_monitor_client.put(f"/api/cameras/{ids[3]}", json={"features": {"gloves": False}})
    assert response.status_code == 200
    assert monitor._workers[ids[3]] is original
    assert original.feature_manager.is_enabled("gloves") is False
    assert all(worker.feature_manager.is_enabled("gloves") for worker in workers if worker is not original)


def test_camera_padrao_respeita_regras_do_banco_e_features_acompanha_troca(real_monitor_app, real_monitor_client):
    monitor = real_monitor_app.extensions["monitor_service"]
    primeira = _create(real_monitor_client, features={"helmet": False}).get_json()["id"]
    segunda = _create(real_monitor_client, name="Segunda", source="1", features={"vest": False}).get_json()["id"]
    assert monitor._workers[primeira].feature_manager.is_enabled("helmet") is False
    real_monitor_client.delete(f"/api/cameras/{primeira}")
    response = real_monitor_client.patch("/features", json={"features": {"gloves": False}})
    assert response.status_code == 200
    assert monitor._workers[segunda].feature_manager.is_enabled("gloves") is False
    assert monitor._workers[segunda].feature_manager.is_enabled("vest") is False
    with real_monitor_app.app_context():
        assert Camera.query.get(segunda).features_json["gloves"] is False
