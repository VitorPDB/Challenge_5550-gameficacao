"""Regressões da auditoria multiagente do PR #7.

Cada teste aqui trava um problema que a auditoria confirmou reproduzindo. São
falhas que a suíte original NÃO pegava — a maioria por afirmar sobre a intenção
do código em vez de sobre o comportamento observável.
"""
from __future__ import annotations

import pytest
from app import create_app
from app.config import AuthTestConfig, Config
from app.extensions import db
from app.models import ROLE_OPERATOR, ROLE_SUPERVISOR, ROLE_TECHNICAL, User
from app.services.auth_service import AuthService
from flask.sessions import SecureCookieSessionInterface

SENHA = "senha-forte-de-teste"


@pytest.fixture()
def app(tmp_path):
    class Cfg(AuthTestConfig):
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'revog.db'}"

    app = create_app(Cfg)
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


def entrar(client, papel: str, senha: str = SENHA):
    return client.post("/api/auth/login", json={"email": f"{papel}@fabrica.com", "password": senha})


# ============================================== CRÍTICO: chave pública =====
class TestSecretKey:
    """A guarda comparava só com o default do `config.py`. O `.env.example`,
    que o README manda copiar, entrega OUTRO literal público — e a aplicação
    subia com ele, deixando qualquer leitor do repositório forjar a sessão de
    um supervisor sem credencial nenhuma."""

    @pytest.mark.parametrize("chave", ["dev-secret-change-me", "change-me", "changeme", "secret", ""])
    def test_recusa_subir_com_chave_que_esta_no_repositorio(self, chave):
        class Cfg(Config):
            TESTING = False
            DEBUG = False
            AUTH_REQUIRED = True
            AUTO_CREATE_TABLES = False
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SECRET_KEY = chave

        with pytest.raises(RuntimeError, match="SECRET_KEY"):
            create_app(Cfg)

    def test_recusa_chave_curta_demais(self):
        class Cfg(Config):
            TESTING = False
            DEBUG = False
            AUTH_REQUIRED = True
            AUTO_CREATE_TABLES = False
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SECRET_KEY = "curta"

        with pytest.raises(RuntimeError, match="caracteres"):
            create_app(Cfg)

    def test_aceita_chave_de_verdade(self):
        class Cfg(Config):
            TESTING = False
            DEBUG = False
            AUTH_REQUIRED = True
            AUTO_CREATE_TABLES = False
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SECRET_KEY = "a" * 64

        assert create_app(Cfg) is not None

    def test_sem_autenticacao_a_chave_nao_importa(self):
        """A flag existe para a suíte; sem login não há sessão para forjar."""

        class Cfg(Config):
            TESTING = False
            DEBUG = False
            AUTH_REQUIRED = False
            AUTO_CREATE_TABLES = False
            SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
            SECRET_KEY = "change-me"

        assert create_app(Cfg) is not None


# ===================================== ALTO: revogação real de sessão ======
class TestRevogacao:
    def test_trocar_a_senha_derruba_a_sessao_antiga(self, app, client):
        """Resposta padrão a conta comprometida. Antes, o cookie do invasor
        continuava valendo depois do reset."""
        entrar(client, ROLE_TECHNICAL)
        assert client.get("/alerts").status_code == 200

        with app.app_context():
            alvo = User.query.filter_by(email="technical@fabrica.com").first()
            AuthService().set_password(alvo, "outra-senha-bem-longa")

        assert client.get("/alerts").status_code == 401
        assert client.get("/api/auth/me").get_json()["user"] is None

    def test_desativar_derruba_a_sessao(self, app, client):
        entrar(client, ROLE_OPERATOR)
        with app.app_context():
            alvo = User.query.filter_by(email="operator@fabrica.com").first()
            alvo.active = False
            db.session.commit()

        assert client.get("/alerts").status_code == 401

    def test_cookie_copiado_para_de_valer_apos_troca_de_senha(self, app, client):
        """O cenário que motivou a época: cookie capturado ANTES do reset."""
        entrar(client, ROLE_TECHNICAL)
        cookie_roubado = next(c.value for c in client.cookie_jar if c.name == "session") if hasattr(client, "cookie_jar") else None
        if cookie_roubado is None:  # Werkzeug novo não expõe cookie_jar
            with app.app_context():
                alvo = User.query.filter_by(email="technical@fabrica.com").first()
                serializer = SecureCookieSessionInterface().get_signing_serializer(app)
                cookie_roubado = serializer.dumps({"user_id": alvo.id, "epoch": alvo.session_epoch})

        ladrao = app.test_client()
        ladrao.set_cookie("session", cookie_roubado, domain="localhost")
        assert ladrao.get("/alerts").status_code == 200, "o cenário precisa começar válido"

        with app.app_context():
            alvo = User.query.filter_by(email="technical@fabrica.com").first()
            AuthService().set_password(alvo, "senha-nova-bem-longa")

        assert ladrao.get("/alerts").status_code == 401

    def test_logout_nao_expulsa_dos_outros_dispositivos(self, app, client):
        """Decisão deliberada: sair no desktop não pode derrubar o kiosk."""
        entrar(client, ROLE_OPERATOR)
        outro = app.test_client()
        entrar(outro, ROLE_OPERATOR)

        client.post("/api/auth/logout")

        assert client.get("/alerts").status_code == 401
        assert outro.get("/alerts").status_code == 200


# ================================ ALTO: PATCH burlando a senha atual =======
class TestTrocaDeSenha:
    def test_supervisor_nao_troca_a_propria_senha_sem_a_atual(self, app, client):
        """Por PATCH /api/users/<self> dava pra pular a exigência que
        /api/auth/password impõe — justamente para o papel mais privilegiado."""
        entrar(client, ROLE_SUPERVISOR)
        with app.app_context():
            meu_id = User.query.filter_by(email="supervisor@fabrica.com").first().id

        resposta = client.patch(f"/api/users/{meu_id}", json={"password": "senha-nova-sem-provar-nada"})
        assert resposta.status_code == 400
        assert "senha atual" in resposta.get_json()["error"]

    def test_supervisor_ainda_reseta_a_senha_de_outra_pessoa(self, app, client):
        """A trava é só para si mesmo — resetar senha de terceiro é o trabalho
        do supervisor."""
        entrar(client, ROLE_SUPERVISOR)
        with app.app_context():
            alvo_id = User.query.filter_by(email="operator@fabrica.com").first().id

        assert client.patch(f"/api/users/{alvo_id}", json={"password": "senha-resetada-longa"}).status_code == 200
        assert entrar(app.test_client(), ROLE_OPERATOR, "senha-resetada-longa").status_code == 200

    def test_errar_a_senha_atual_nao_tranca_o_login(self, app, client):
        """Auto-DoS: `change_own_password` alimentava o contador de força bruta
        do login, então errar a própria senha 5 vezes trancava a conta."""
        entrar(client, ROLE_TECHNICAL)
        for _ in range(8):
            client.post("/api/auth/password", json={"current_password": "errada", "new_password": "nova-senha-longa"})

        client.post("/api/auth/logout")
        assert entrar(client, ROLE_TECHNICAL).status_code == 200


# ================== ALTO/MÉDIO: AUTH_REQUIRED=False não abre o CRUD =======
class TestFlagNaoAbreGestao:
    @pytest.fixture()
    def app_sem_auth(self, tmp_path):
        from app.config import TestConfig

        class Cfg(TestConfig):
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'sem-auth.db'}"

        app = create_app(Cfg)
        with app.app_context():
            db.create_all()
            db.session.remove()
        yield app
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_nao_da_pra_criar_supervisor_sem_sessao(self, app_sem_auth):
        """A flag existe para a suíte focar no próprio assunto — não para
        deixar qualquer um se promover."""
        resposta = app_sem_auth.test_client().post(
            "/api/users",
            json={"email": "invasor@fora.com", "name": "Invasor", "password": "senha-longa-invasor", "role": ROLE_SUPERVISOR},
        )
        assert resposta.status_code == 401

    def test_gestao_de_usuarios_toda_fechada(self, app_sem_auth):
        c = app_sem_auth.test_client()
        assert c.get("/api/users").status_code == 401
        assert c.patch("/api/users/1", json={"role": ROLE_SUPERVISOR}).status_code == 401
        assert c.delete("/api/users/1").status_code == 401

    def test_trocar_senha_responde_401_em_vez_de_estourar(self, app_sem_auth):
        """Era a única rota da aplicação que devolvia 500 nesse modo."""
        resposta = app_sem_auth.test_client().post(
            "/api/auth/password", json={"current_password": "x", "new_password": "y"}
        )
        assert resposta.status_code == 401


# ======================================= ALTO: socket revalida a sessão ====
class TestSocket:
    def test_socket_cai_quando_a_sessao_e_revogada(self, app):
        """Só validar no `connect` deixava o feed ao vivo (vídeo, alertas,
        pessoas detectadas) chegando a quem já tinha perdido o acesso."""
        from app.extensions import socketio

        flask_client = app.test_client()
        entrar(flask_client, ROLE_TECHNICAL)
        ws = socketio.test_client(app, flask_test_client=flask_client)
        assert ws.is_connected()

        with app.app_context():
            alvo = User.query.filter_by(email="technical@fabrica.com").first()
            AuthService().set_password(alvo, "senha-nova-bem-longa")

        assert not ws.is_connected(), "o servidor deve derrubar o socket sem cooperação do cliente"

    def test_socket_sobrevive_a_revalidacao_com_sessao_valida(self, app):
        from app.extensions import socketio

        flask_client = app.test_client()
        entrar(flask_client, ROLE_TECHNICAL)
        ws = socketio.test_client(app, flask_test_client=flask_client)

        ws.emit("revalidate")
        assert ws.is_connected()
