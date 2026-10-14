"""Autenticação real: login, papéis e proteção de rota.

Roda com `AUTH_REQUIRED=True` (ver AuthTestConfig) — o resto da suíte usa
`AUTH_REQUIRED=False` para focar no próprio assunto. É aqui que a autenticação
é exercitada de ponta a ponta.
"""
from __future__ import annotations

import re

import pytest
from app import create_app
from app.config import AuthTestConfig
from app.extensions import db
from app.models import ROLE_OPERATOR, ROLE_SUPERVISOR, ROLE_TECHNICAL, User
from app.services.auth_service import MAX_FAILED_ATTEMPTS, AuthError, AuthService, WeakPassword

SENHA = "senha-forte-de-teste"


@pytest.fixture()
def app(tmp_path):
    """App SEM app_context aberto durante o teste.

    O resto da suite mantem um contexto ativo o tempo todo (`:memory:` some se
    a conexao cair), mas aqui isso mascararia a autenticacao: `g` vive no app
    context, e `current_user()` guarda o usuario em `g`. Com um contexto unico
    reaproveitado por todos os requests, o cache nunca expiraria e o teste de
    "desativar derruba a sessao" passaria por acidente.

    Em producao cada request abre o proprio contexto — entao aqui o banco vai
    para arquivo, e cada request roda como roda de verdade.
    """
    class Config(AuthTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'auth-test.db'}"

    app = create_app(Config)
    with app.app_context():
        db.create_all()
        for papel in (ROLE_OPERATOR, ROLE_TECHNICAL, ROLE_SUPERVISOR):
            AuthService().create_user(email=f"{papel}@fabrica.com", name=papel.title(), password=SENHA, role=papel)
        db.session.remove()

    yield app

    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def entrar(client, papel: str):
    resposta = client.post("/api/auth/login", json={"email": f"{papel}@fabrica.com", "password": SENHA})
    assert resposta.status_code == 200, resposta.get_json()
    return resposta


# ------------------------------------------------------------------ login --
def test_login_com_credenciais_validas(client):
    corpo = entrar(client, ROLE_TECHNICAL).get_json()
    assert corpo["user"]["email"] == "technical@fabrica.com"
    assert corpo["user"]["role"] == ROLE_TECHNICAL


def test_resposta_de_login_nunca_traz_o_hash(client):
    """O dicionário do usuário vai pro navegador; hash não pode vazar."""
    corpo = entrar(client, ROLE_OPERATOR).get_json()
    assert "password_hash" not in corpo["user"]
    assert "password" not in corpo["user"]


def test_senha_errada_e_email_inexistente_dao_a_mesma_resposta(client):
    """Não pode dar pra descobrir quais e-mails existem."""
    errada = client.post("/api/auth/login", json={"email": "technical@fabrica.com", "password": "outra-coisa"})
    inexistente = client.post("/api/auth/login", json={"email": "ninguem@fabrica.com", "password": "outra-coisa"})

    assert errada.status_code == inexistente.status_code == 401
    assert errada.get_json() == inexistente.get_json()


def test_me_sem_sessao_devolve_usuario_nulo(client):
    """200 com user null, não 401: é a rota que decide se mostra o login."""
    resposta = client.get("/api/auth/me")
    assert resposta.status_code == 200
    assert resposta.get_json() == {"user": None}


def test_logout_encerra_a_sessao(client):
    entrar(client, ROLE_TECHNICAL)
    assert client.get("/api/auth/me").get_json()["user"] is not None

    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").get_json()["user"] is None


def test_usuario_desativado_nao_entra(app, client):
    with app.app_context():
        usuario = User.query.filter_by(email="operator@fabrica.com").first()
        usuario.active = False
        db.session.commit()

    resposta = client.post("/api/auth/login", json={"email": "operator@fabrica.com", "password": SENHA})
    assert resposta.status_code == 401


def test_desativar_derruba_sessao_ja_aberta(app, client):
    """Não existe token válido fora do banco: o próximo request já cai."""
    entrar(client, ROLE_OPERATOR)
    with app.app_context():
        usuario = User.query.filter_by(email="operator@fabrica.com").first()
        usuario.active = False
        db.session.commit()

    assert client.get("/api/auth/me").get_json()["user"] is None
    assert client.get("/alerts").status_code == 401


# ------------------------------------------------------------ forca bruta --
def test_conta_trava_apos_tentativas_seguidas(client):
    for _ in range(MAX_FAILED_ATTEMPTS):
        client.post("/api/auth/login", json={"email": "technical@fabrica.com", "password": "errada"})

    # Agora nem a senha CERTA passa.
    resposta = client.post("/api/auth/login", json={"email": "technical@fabrica.com", "password": SENHA})
    assert resposta.status_code == 429
    assert "retry_after" in resposta.get_json()


def test_login_certo_zera_o_contador(app, client):
    for _ in range(MAX_FAILED_ATTEMPTS - 1):
        client.post("/api/auth/login", json={"email": "technical@fabrica.com", "password": "errada"})
    entrar(client, ROLE_TECHNICAL)

    with app.app_context():
        usuario = User.query.filter_by(email="technical@fabrica.com").first()
        assert usuario.failed_attempts == 0
        assert usuario.locked_until is None


# ----------------------------------------------------------------- papeis --
@pytest.mark.parametrize(
    "papel,esperado",
    [(ROLE_OPERATOR, 403), (ROLE_TECHNICAL, 201), (ROLE_SUPERVISOR, 201)],
)
def test_cadastrar_camera_exige_tecnico(client, papel, esperado):
    """Hierárquico: supervisor passa onde técnico passa."""
    entrar(client, papel)
    resposta = client.post("/api/cameras", json={"name": "X", "source_type": "USB", "source": "0"})
    assert resposta.status_code == esperado


def test_operador_pode_marcar_falso_positivo(app, client):
    """É a ação dele no kiosk — precisa passar.

    O operador precisa ter SETOR atribuído: sem câmera, por desenho, ele não
    enxerga alerta nenhum (ver tests/test_escopo_camera.py).
    """
    from app.repositories.alert_repository import AlertRepository

    with app.app_context():
        operador = User.query.filter_by(email="operator@fabrica.com").first()
        operador.camera_id = 7
        db.session.commit()
        alerta_id = AlertRepository().create(
            rule="missing_helmet", severity="critical", message="m", feature="helmet",
            camera_id=7, metadata={"person_id": "person_1"},
        ).id

    entrar(client, ROLE_OPERATOR)
    assert client.post(f"/alerts/{alerta_id}/false-positive", json={"reason": "reflexo"}).status_code == 200


def test_403_explica_o_que_faltou(client):
    entrar(client, ROLE_OPERATOR)
    corpo = client.patch("/settings", json={"target_fps": 20}).get_json()

    assert corpo["required_role"] == ROLE_TECHNICAL
    assert corpo["your_role"] == ROLE_OPERATOR


def test_gestao_de_usuarios_e_so_do_supervisor(client):
    entrar(client, ROLE_TECHNICAL)
    assert client.get("/api/users").status_code == 403

    client.post("/api/auth/logout")
    entrar(client, ROLE_SUPERVISOR)
    assert client.get("/api/users").status_code == 200


def test_supervisor_nao_se_rebaixa_nem_se_desativa(app, client):
    """Trava contra ficar sem ninguém capaz de gerir usuários."""
    entrar(client, ROLE_SUPERVISOR)
    with app.app_context():
        meu_id = User.query.filter_by(email="supervisor@fabrica.com").first().id

    assert client.patch(f"/api/users/{meu_id}", json={"role": ROLE_OPERATOR}).status_code == 400
    assert client.patch(f"/api/users/{meu_id}", json={"active": False}).status_code == 400
    assert client.delete(f"/api/users/{meu_id}").status_code == 400


# ------------------------------------------------ varredura de protecao ----
# Rotas publicas POR DECISAO, com o motivo. Qualquer outra rota tem que exigir
# sessao — o teste abaixo falha se aparecer uma nova sem proteger.
PUBLICAS = {
    "auth.login",       # obviamente
    "auth.logout",      # idempotente, nao expoe nada
    "auth.me",          # e quem decide se a tela de login aparece
    "status.index",     # shell do SPA (o HTML em si nao tem dado)
    "static",           # assets do build
}


def test_toda_rota_exige_sessao(app, client):
    """Varredura: nenhuma rota da API pode responder sem login.

    Pega o esquecimento clássico — alguém adiciona um endpoint e não coloca o
    decorador. Sem isto, a proteção depende de lembrar.
    """
    desprotegidas = []
    for regra in app.url_map.iter_rules():
        if regra.endpoint in PUBLICAS:
            continue
        metodo = "GET" if "GET" in regra.methods else sorted(regra.methods - {"HEAD", "OPTIONS"})[0]
        caminho = re.sub(r"<([^>]+)>", lambda match: "1" if match[1].startswith("int:") else "x.jpg", regra.rule)
        resposta = client.open(caminho, method=metodo)
        if resposta.status_code != 401:
            desprotegidas.append(f"{metodo} {regra.rule} -> {resposta.status_code}")

    assert not desprotegidas, "rotas acessíveis sem login: " + ", ".join(desprotegidas)


def test_websocket_rejeita_sem_sessao(app):
    """O feed de análise/alertas trafega por socket: proteger só o REST
    deixaria a porta dos fundos aberta."""
    from app.extensions import socketio

    cliente_ws = socketio.test_client(app, flask_test_client=app.test_client())
    assert not cliente_ws.is_connected()


def test_websocket_aceita_com_sessao(app):
    flask_client = app.test_client()
    flask_client.post("/api/auth/login", json={"email": "technical@fabrica.com", "password": SENHA})

    from app.extensions import socketio

    cliente_ws = socketio.test_client(app, flask_test_client=flask_client)
    assert cliente_ws.is_connected()


# -------------------------------------------------------------- servico ----
def test_senha_curta_e_recusada(app):
    with app.app_context():
        with pytest.raises(WeakPassword):
            AuthService().create_user(email="novo@fabrica.com", name="Novo", password="curta", role=ROLE_OPERATOR)


def test_email_duplicado_e_recusado(app):
    with app.app_context():
        with pytest.raises(ValueError, match="Já existe"):
            AuthService().create_user(
                email="technical@fabrica.com", name="Outro", password=SENHA, role=ROLE_TECHNICAL
            )


def test_email_normalizado_no_cadastro_e_no_login(app, client):
    with app.app_context():
        usuario = AuthService().create_user(
            email="  MAIUSCULA@Fabrica.COM ", name="Teste", password=SENHA, role=ROLE_OPERATOR
        )
        assert usuario.email == "maiuscula@fabrica.com"

    assert client.post("/api/auth/login", json={"email": "MAIUSCULA@FABRICA.COM", "password": SENHA}).status_code == 200


def test_hash_nao_e_a_senha(app):
    with app.app_context():
        usuario = User.query.filter_by(email="operator@fabrica.com").first()
        assert SENHA not in usuario.password_hash
        assert usuario.password_hash.startswith("scrypt:")


def test_authenticate_devolve_erro_generico(app):
    with app.app_context():
        with pytest.raises(AuthError) as exc:
            AuthService().authenticate("ninguem@fabrica.com", "seja-o-que-for")
        assert "inválidos" in exc.value.message


def test_supervisor_cria_operador_so_com_camera_cadastrada(app, client):
    from app.models import Camera

    with app.app_context():
        db.session.add(Camera(name="Portaria", source_type="USB", source="0"))
        db.session.commit()
        cam_id = Camera.query.first().id

    entrar(client, ROLE_SUPERVISOR)
    corpo = {"email": "novo@fabrica.com", "name": "Novo", "password": SENHA, "role": "operator"}
    assert client.post("/api/users", json=corpo).status_code == 400  # sem câmera
    assert client.post("/api/users", json=corpo | {"camera_id": 9999}).status_code == 400  # não existe
    assert client.post("/api/users", json=corpo | {"camera_id": cam_id}).status_code == 201


@pytest.mark.parametrize(
    "change", [{"active": False}, {"role": ROLE_OPERATOR}, {"camera_id": None}, {"password": "nova-senha-forte"}, "delete"]
)
def test_access_change_disconnects_open_socket_without_client_revalidation(app, change):
    from app.extensions import socketio
    from app.models import Camera

    with app.app_context():
        camera = Camera(name="Área A", source="0", source_type="USB")
        db.session.add(camera)
        user = User.query.filter_by(email="technical@fabrica.com").first()
        user.camera_id = 1
        db.session.commit()
        user_id = user.id
    target = app.test_client()
    entrar(target, ROLE_TECHNICAL)
    ws = socketio.test_client(app, flask_test_client=target)
    assert ws.is_connected()
    admin = app.test_client()
    entrar(admin, ROLE_SUPERVISOR)
    response = admin.delete(f"/api/users/{user_id}") if change == "delete" else admin.patch(f"/api/users/{user_id}", json=change)
    assert response.status_code == 200
    assert not ws.is_connected()
    assert target.get("/status").status_code == 401


@pytest.mark.parametrize("legacy", [False, True])
def test_revoked_user_stops_receiving_existing_video_stream(app, monkeypatch, legacy):
    from app.models import Camera

    with app.app_context():
        camera = Camera(name="Área A", source="0", source_type="USB")
        db.session.add(camera)
        db.session.commit()
        camera_id = camera.id
        user_id = User.query.filter_by(email="technical@fabrica.com").first().id
    monitor = app.extensions["monitor_service"]
    monkeypatch.setattr(monitor, "latest_jpeg", lambda **kwargs: b"image")
    monkeypatch.setattr(monitor, "latest_jpeg_versionado", lambda **kwargs: (b"image", 1))
    target = app.test_client()
    entrar(target, ROLE_TECHNICAL)
    response = target.get("/video_feed" if legacy else f"/api/cameras/{camera_id}/video_feed", buffered=False)
    chunks = iter(response.response)
    assert b"image" in next(chunks)
    admin = app.test_client()
    entrar(admin, ROLE_SUPERVISOR)
    assert admin.patch(f"/api/users/{user_id}", json={"active": False}).status_code == 200
    with pytest.raises(StopIteration):
        next(chunks)
    response.close()
