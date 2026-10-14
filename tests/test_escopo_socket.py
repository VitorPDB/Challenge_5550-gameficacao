"""Escopo de câmera no WebSocket.

`tests/test_escopo_camera.py` fixa o escopo do Operador no REST: 22 testes
garantindo que o operador da Portaria não lista, não vê vídeo, não para e não
baixa evidência da câmera do Almoxarifado.

Só que o escopo parava no REST. Nenhum `socketio.emit` do projeto usava
`room=` — todo evento era broadcast para todos os sockets conectados. Na
prática: o operador da Portaria, que recebe 404 ao pedir a câmera do
Almoxarifado por HTTP, recebia `analysis`, `compliance_state` e `active_alerts`
do Almoxarifado pela porta dos fundos, ~12 vezes por segundo.

O README afirmava que o operador "só vê a câmera do setor dele". Esta suíte é
o que faz a afirmação ser verdadeira também no socket.
"""

from __future__ import annotations

import pytest
from app import create_app
from app.config import AuthTestConfig
from app.extensions import db, socketio
from app.models import ROLE_OPERATOR, ROLE_TECHNICAL, Camera, User
from app.repositories.alert_repository import AlertRepository
from app.services.alert_state_service import AlertStateService
from app.services.auth_service import AuthService
from app.services.risk_rules import RuleAlert

SENHA = "senha-forte-de-teste"


@pytest.fixture()
def app(tmp_path):
    class Cfg(AuthTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'escopo_socket.db'}"

    aplicacao = create_app(Cfg)
    with aplicacao.app_context():
        db.create_all()
        portaria = Camera(name="Portaria", source_type="USB", source="0")
        almox = Camera(name="Almoxarifado", source_type="USB", source="1")
        db.session.add_all([portaria, almox])
        db.session.commit()
        aplicacao.config["ID_PORTARIA"] = portaria.id
        aplicacao.config["ID_ALMOX"] = almox.id

        AuthService().create_user(
            email="ana@fabrica.com", name="Ana", password=SENHA, role=ROLE_OPERATOR, camera_id=portaria.id
        )
        AuthService().create_user(
            email="bruno@fabrica.com", name="Bruno", password=SENHA, role=ROLE_OPERATOR, camera_id=almox.id
        )
        AuthService().create_user(email="semsetor@fabrica.com", name="Sem Setor", password=SENHA, role=ROLE_OPERATOR)
        AuthService().create_user(email="tec@fabrica.com", name="Tec", password=SENHA, role=ROLE_TECHNICAL)
        yield aplicacao
        db.session.remove()
        db.drop_all()


def conectar(aplicacao, email: str):
    """Abre um socket autenticado como `email`, do jeito que o navegador faz."""
    cliente_http = aplicacao.test_client()
    resposta = cliente_http.post("/api/auth/login", json={"email": email, "password": SENHA})
    assert resposta.status_code == 200, f"login de {email} falhou: {resposta.status_code}"
    cliente_ws = socketio.test_client(aplicacao, flask_test_client=cliente_http)
    assert cliente_ws.is_connected(), f"socket de {email} nao conectou"
    cliente_ws.get_received()  # descarta o que chegou durante o handshake
    return cliente_ws


def eventos(cliente_ws, nome: str) -> list[dict]:
    return [evento["args"][0] for evento in cliente_ws.get_received() if evento["name"] == nome]


def emitir_alertas_da_camera(aplicacao, camera_id: int) -> None:
    """Dispara um `active_alerts` pelo caminho de producao, nao por atalho.

    `AlertStateService.process` emite `active_alerts` ao final de TODA chamada
    (`alert_state_service.py:153`) — e e esse o emit que roda ~12x por segundo
    dentro do loop de captura. E ele que precisa respeitar o escopo.
    """
    with aplicacao.app_context():
        servico = AlertStateService(AlertRepository(), socketio, camera_id=camera_id)
        servico.process([])


def test_operador_nao_recebe_alertas_de_outro_setor(app):
    """O caso que o README prometia e o socket entregava errado."""
    ana = conectar(app, "ana@fabrica.com")  # Portaria
    bruno = conectar(app, "bruno@fabrica.com")  # Almoxarifado

    emitir_alertas_da_camera(app, app.config["ID_ALMOX"])

    recebidos_ana = eventos(ana, "active_alerts")
    recebidos_bruno = eventos(bruno, "active_alerts")

    assert recebidos_bruno, "Bruno e do Almoxarifado e precisava receber o evento da propria camera"
    assert all(evento["camera_id"] == app.config["ID_ALMOX"] for evento in recebidos_bruno)
    assert recebidos_ana == [], (
        f"Ana e operadora da Portaria e recebeu {len(recebidos_ana)} evento(s) do Almoxarifado pelo socket. "
        "O escopo de camera existe no REST e vazava no WebSocket."
    )


def test_cada_operador_recebe_a_propria_camera(app):
    """A contraprova: o escopo nao pode ser 'ninguem recebe nada'."""
    ana = conectar(app, "ana@fabrica.com")
    bruno = conectar(app, "bruno@fabrica.com")

    emitir_alertas_da_camera(app, app.config["ID_PORTARIA"])

    assert eventos(ana, "active_alerts"), "Ana precisava receber o evento da camera dela"
    assert eventos(bruno, "active_alerts") == [], "Bruno recebeu evento da Portaria"


def test_tecnico_recebe_de_todas_as_cameras(app):
    """Tecnico e Supervisor veem o parque inteiro — e o trabalho deles."""
    tecnico = conectar(app, "tec@fabrica.com")

    emitir_alertas_da_camera(app, app.config["ID_PORTARIA"])
    emitir_alertas_da_camera(app, app.config["ID_ALMOX"])

    recebidos = eventos(tecnico, "active_alerts")
    camaras = {evento["camera_id"] for evento in recebidos}
    assert camaras == {app.config["ID_PORTARIA"], app.config["ID_ALMOX"]}, (
        f"Tecnico deveria ver as duas cameras; viu {camaras}"
    )


def test_operador_sem_setor_nao_recebe_nada(app):
    """Conta incompleta nao vira acesso amplo.

    Mesma decisao ja tomada no REST (`test_escopo_camera.py::TestSemSetor`):
    deixar passar daria acesso ao parque inteiro justamente para a conta que
    ninguem terminou de configurar.
    """
    orfao = conectar(app, "semsetor@fabrica.com")

    emitir_alertas_da_camera(app, app.config["ID_PORTARIA"])
    emitir_alertas_da_camera(app, app.config["ID_ALMOX"])

    assert eventos(orfao, "active_alerts") == [], "Operador sem setor recebeu evento pelo socket"


def test_escopo_do_socket_acompanha_a_troca_de_setor(app):
    """Supervisor remaneja o operador; o socket novo tem que seguir o setor novo."""
    with app.app_context():
        ana = User.query.filter_by(email="ana@fabrica.com").first()
        ana.camera_id = app.config["ID_ALMOX"]
        db.session.commit()

    cliente = conectar(app, "ana@fabrica.com")
    emitir_alertas_da_camera(app, app.config["ID_ALMOX"])
    assert eventos(cliente, "active_alerts"), "Ana foi remanejada para o Almoxarifado e nao recebeu"

    emitir_alertas_da_camera(app, app.config["ID_PORTARIA"])
    assert eventos(cliente, "active_alerts") == [], "Ana ainda recebe da Portaria depois de sair de la"


def test_falso_positivo_na_camera_secundaria_nao_emite_alerta_da_padrao(app):
    ana = conectar(app, "ana@fabrica.com")
    bruno = conectar(app, "bruno@fabrica.com")
    with app.app_context():
        monitor = app.extensions["monitor_service"]
        monitor.load_cameras_from_db()
        criados = {}
        for camera_id in (app.config["ID_PORTARIA"], app.config["ID_ALMOX"]):
            state = monitor._get_worker(camera_id).alert_state_service
            state.create_after_frames = 1
            criados[camera_id] = state.process([
                RuleAlert("missing_helmet", "critical", "Sem capacete", "helmet", {"person_id": "person_1"})
            ])["created"][0]["id"]
    ana.get_received()
    bruno.get_received()
    cliente = app.test_client()
    assert cliente.post("/api/auth/login", json={"email": "bruno@fabrica.com", "password": SENHA}).status_code == 200
    alvo = criados[app.config["ID_ALMOX"]]
    resposta = cliente.post(f"/alerts/{alvo}/false-positive", json={"reason": "revisado"})
    assert resposta.status_code == 200
    with app.app_context():
        state = monitor._workers[app.config["ID_ALMOX"]].alert_state_service
        assert state.active_alerts() == []
        assert state.process([], deteccao_nova=False)["active"] == []
    assert eventos(ana, "active_alerts") == []
    recebidos = eventos(bruno, "active_alerts")
    assert recebidos
    for payload in recebidos:
        assert payload["camera_id"] == app.config["ID_ALMOX"]
        assert all(item["camera_id"] == payload["camera_id"] for item in payload["items"])
        assert all(item["id"] != alvo for item in payload["items"])
