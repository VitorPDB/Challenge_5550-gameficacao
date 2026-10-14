"""Escopo de câmera do Operador.

`User.camera_id` existia desde o PR da autenticação, mas nenhuma rota o
aplicava: o operador da Portaria enxergava — e podia parar — a câmera do
Almoxarifado. A auditoria multiagente flagrou o campo como "promete mais do que
entrega". Estes testes fixam o que ele passa a valer.

Regra: só o Operador é restrito, e à câmera do setor dele. Técnico e Supervisor
veem o parque inteiro, porque é o trabalho deles.
"""
from __future__ import annotations

import pytest
from app import create_app
from app.config import AuthTestConfig
from app.extensions import db
from app.models import ROLE_OPERATOR, ROLE_SUPERVISOR, ROLE_TECHNICAL, Camera, User
from app.repositories.alert_repository import AlertRepository
from app.services.auth_service import AuthService

SENHA = "senha-forte-de-teste"


@pytest.fixture()
def app(tmp_path):
    class Cfg(AuthTestConfig):
        SNAPSHOT_DIR = str(tmp_path / "snapshots")
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'escopo.db'}"

    app = create_app(Cfg)
    with app.app_context():
        db.create_all()
        portaria = Camera(name="Portaria", source_type="USB", source="0")
        almox = Camera(name="Almoxarifado", source_type="USB", source="1")
        db.session.add_all([portaria, almox])
        db.session.commit()
        app.config["ID_PORTARIA"] = portaria.id
        app.config["ID_ALMOX"] = almox.id
        # As cameras foram inseridas direto no banco; sem isto o MonitorService
        # nao tem worker pra elas e as rotas respondem 409.
        app.extensions["monitor_service"].load_cameras_from_db()

        AuthService().create_user(
            email="ana@fabrica.com", name="Ana", password=SENHA, role=ROLE_OPERATOR, camera_id=portaria.id
        )
        AuthService().create_user(email="semsetor@fabrica.com", name="Sem Setor", password=SENHA, role=ROLE_OPERATOR)
        AuthService().create_user(email="tec@fabrica.com", name="Tec", password=SENHA, role=ROLE_TECHNICAL)
        AuthService().create_user(email="sup@fabrica.com", name="Sup", password=SENHA, role=ROLE_SUPERVISOR)

        AlertRepository().create(
            rule="missing_helmet", severity="critical", message="na portaria",
            feature="helmet", camera_id=portaria.id, metadata={"person_id": "person_1"},
        )
        AlertRepository().create(
            rule="missing_vest", severity="high", message="no almoxarifado",
            feature="vest", camera_id=almox.id, metadata={"person_id": "person_1"},
        )
        db.session.remove()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def entrar(app, email: str):
    c = app.test_client()
    assert c.post("/api/auth/login", json={"email": email, "password": SENHA}).status_code == 200
    return c


@pytest.fixture()
def ana(app):
    """Operadora da Portaria."""
    return entrar(app, "ana@fabrica.com")


@pytest.fixture()
def tecnico(app):
    return entrar(app, "tec@fabrica.com")


# ============================================================= câmeras =====
def test_operador_lista_so_a_camera_do_setor(app, ana):
    corpo = ana.get("/api/cameras").get_json()
    assert corpo["count"] == 1
    assert corpo["items"][0]["name"] == "Portaria"


def test_tecnico_lista_o_parque_inteiro(app, tecnico):
    assert tecnico.get("/api/cameras").get_json()["count"] == 2


def test_operador_nao_alcanca_camera_de_outra_area(app, ana):
    outra = app.config["ID_ALMOX"]
    # 404, nao 403: pra ela aquela camera simplesmente nao existe.
    assert ana.get(f"/api/cameras/{outra}").status_code == 404
    assert ana.get(f"/api/cameras/{outra}/status").status_code == 404
    assert ana.get(f"/api/cameras/{outra}/analysis").status_code == 404


def test_operador_nao_para_camera_de_outra_area(app, ana):
    """O pior caso: derrubar o monitoramento de um setor que não é o dele."""
    outra = app.config["ID_ALMOX"]
    assert ana.post(f"/api/cameras/{outra}/stop").status_code == 404
    assert ana.post(f"/api/cameras/{outra}/start").status_code == 404


def test_operador_nao_ve_o_video_de_outra_area(app, ana):
    """Feed é o dado mais sensível: imagem de pessoas trabalhando."""
    assert ana.get(f"/api/cameras/{app.config['ID_ALMOX']}/video_feed").status_code == 404
    assert ana.get(f"/api/cameras/{app.config['ID_ALMOX']}/frame.jpg").status_code == 404


def test_operador_opera_normalmente_a_propria_camera(app, ana):
    minha = app.config["ID_PORTARIA"]
    assert ana.get(f"/api/cameras/{minha}").status_code == 200
    assert ana.get(f"/api/cameras/{minha}/status").status_code == 200
    assert ana.post(f"/api/cameras/{minha}/stop").status_code == 200


# ============================================================== alertas ====
def test_operador_ve_so_os_alertas_do_setor(app, ana):
    corpo = ana.get("/alerts").get_json()
    assert corpo["count"] == 1
    assert corpo["items"][0]["message"] == "na portaria"


def test_escopo_vence_o_parametro_da_query(app, ana):
    """Pedir ?camera_id de outra área não amplia o que ela vê."""
    corpo = ana.get(f"/alerts?camera_id={app.config['ID_ALMOX']}").get_json()
    assert corpo["count"] == 1
    assert corpo["items"][0]["message"] == "na portaria"


def test_operador_nao_age_sobre_alerta_de_outra_area(app, ana):
    with app.app_context():
        alerta = AlertRepository().list_recent(camera_id=app.config["ID_ALMOX"])[0]
        alerta_id = alerta.id

    assert ana.post(f"/alerts/{alerta_id}/false-positive", json={"reason": "x"}).status_code == 404
    assert ana.post(f"/alerts/{alerta_id}/acknowledge", json={}).status_code == 404


def test_operador_nao_baixa_evidencia_de_outra_area(app, ana):
    """A evidência é uma FOTO de alguém trabalhando."""
    with app.app_context():
        alerta = AlertRepository().list_recent(camera_id=app.config["ID_ALMOX"])[0]
        AlertRepository().update_frame_ref(alerta, "/snapshots/x.jpg")
        alerta_id = alerta.id

    assert ana.get(f"/alerts/{alerta_id}/evidence").status_code == 404


def test_tecnico_ve_alertas_de_todas(app, tecnico):
    assert tecnico.get("/alerts").get_json()["count"] == 2


# ========================================================= score e eventos =
def test_score_do_operador_e_do_setor_dele(app, ana, tecnico):
    assert ana.get("/risk-score").get_json()["camera_id"] == app.config["ID_PORTARIA"]
    # Técnico sem parâmetro = consolidado.
    assert tecnico.get("/risk-score").get_json()["camera_id"] is None


def test_score_ignora_camera_id_de_outra_area(app, ana):
    corpo = ana.get(f"/risk-score?camera_id={app.config['ID_ALMOX']}").get_json()
    assert corpo["camera_id"] == app.config["ID_PORTARIA"]


# =================================================== rotas legadas ========
def test_rotas_legadas_apontam_pra_camera_do_operador(app, ana):
    """`/status` operava sobre a câmera de MENOR ID. Para um operador de outro
    setor, isso era a câmera errada — e ele podia pará-la por `/stop`."""
    corpo = ana.get("/status").get_json()
    assert corpo["camera_id"] == app.config["ID_PORTARIA"]


def test_status_do_tecnico_segue_na_camera_padrao(app, tecnico):
    assert tecnico.get("/status").get_json()["camera_id"] == app.config["ID_PORTARIA"]


# ============================================== operador ainda sem setor ===
class TestSemSetor:
    """Conta criada mas sem câmera atribuída ainda.

    Não vê NADA. Deixar passar seria pior do que o problema resolvido aqui: a
    conta teria acesso amplo justamente por estar incompleta.
    """

    @pytest.fixture()
    def orfa(self, app):
        return entrar(app, "semsetor@fabrica.com")

    def test_lista_de_cameras_vem_vazia(self, orfa):
        assert orfa.get("/api/cameras").get_json() == {"items": [], "count": 0}

    def test_nao_alcanca_nenhuma_camera(self, app, orfa):
        assert orfa.get(f"/api/cameras/{app.config['ID_PORTARIA']}").status_code == 403

    def test_erro_explica_o_que_fazer(self, app, orfa):
        corpo = orfa.get(f"/api/cameras/{app.config['ID_PORTARIA']}").get_json()
        assert corpo["code"] == "sem_camera_atribuida"
        assert "supervisor" in corpo["error"].lower()

    def test_rotas_legadas_nao_caem_na_camera_padrao(self, orfa):
        """O furo mais fácil de passar despercebido: sem escopo, `/status`
        entregaria a câmera de menor id."""
        assert orfa.get("/status").status_code == 403
        assert orfa.post("/start").status_code == 403

    def test_nao_ve_alerta_nenhum(self, orfa):
        assert orfa.get("/alerts").get_json()["count"] == 0


# ================================================ supervisor sem restrição =
def test_supervisor_ve_tudo(app):
    sup = entrar(app, "sup@fabrica.com")
    assert sup.get("/api/cameras").get_json()["count"] == 2
    assert sup.get("/alerts").get_json()["count"] == 2
    assert sup.get(f"/api/cameras/{app.config['ID_ALMOX']}").status_code == 200


def test_atribuir_setor_muda_o_que_a_pessoa_ve(app):
    """O supervisor move alguém de área e o acesso acompanha."""
    sup = entrar(app, "sup@fabrica.com")
    with app.app_context():
        ana_id = User.query.filter_by(email="ana@fabrica.com").first().id

    assert sup.patch(f"/api/users/{ana_id}", json={"camera_id": app.config["ID_ALMOX"]}).status_code == 200

    ana = entrar(app, "ana@fabrica.com")
    corpo = ana.get("/api/cameras").get_json()
    assert corpo["count"] == 1
    assert corpo["items"][0]["name"] == "Almoxarifado"


@pytest.mark.parametrize("path", ["/risk-score", "/risk-score/trend"])
@pytest.mark.parametrize("query", ["", "?camera_id=1", "?camera_id=2"])
def test_risco_sem_setor_nao_entrega_o_parque(app, path, query):
    cliente = entrar(app, "semsetor@fabrica.com")
    resposta = cliente.get(path + query)
    assert resposta.status_code == 403
    assert resposta.get_json()["code"] == "sem_camera_atribuida"


@pytest.fixture()
def evidencias(app):
    from pathlib import Path

    directory = Path(app.config["SNAPSHOT_DIR"])
    directory.mkdir()
    with app.app_context():
        for camera_id, filename in [(app.config["ID_PORTARIA"], "portaria.jpg"), (app.config["ID_ALMOX"], "almox.jpg")]:
            alert = AlertRepository().list_recent(camera_id=camera_id)[0]
            AlertRepository().update_frame_ref(alert, f"/snapshots/{filename}")
            (directory / filename).write_bytes(filename.encode())
        (directory / "orfao.jpg").write_bytes(b"sem alerta")
    return directory


def test_snapshot_direto_respeita_setor(app, ana, evidencias):
    assert ana.get("/snapshots/portaria.jpg").data == b"portaria.jpg"
    assert ana.get("/snapshots/almox.jpg").status_code == 404


def test_snapshot_direto_sem_setor_e_negado(app, evidencias):
    cliente = entrar(app, "semsetor@fabrica.com")
    assert cliente.get("/snapshots/portaria.jpg").status_code == 403


def test_snapshot_sem_alerta_nao_e_publicado(tecnico, evidencias):
    assert tecnico.get("/snapshots/orfao.jpg").status_code == 404


@pytest.mark.parametrize("path", ["/snapshots/almox.jpg", "/alerts/2/evidence"])
def test_evidencia_historica_independe_de_worker(app, tecnico, evidencias, path):
    with app.app_context():
        Camera.query.delete()
        db.session.commit()
        app.extensions["monitor_service"].load_cameras_from_db()
    resposta = tecnico.get(path)
    assert resposta.status_code == 200
    assert resposta.data == b"almox.jpg"
    assert "no-store" in resposta.headers["Cache-Control"]


def test_snapshot_sem_sessao_e_negado(app, evidencias):
    assert app.test_client().get("/snapshots/portaria.jpg").status_code == 401


def test_features_sem_camera_explicitada_respeitam_operador(app, ana):
    response = ana.get("/features")
    assert response.status_code == 200
    assert response.get_json()["camera_id"] == app.config["ID_PORTARIA"]
    assert entrar(app, "semsetor@fabrica.com").get("/features").status_code == 403
