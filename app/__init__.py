from __future__ import annotations

import logging

from flask import Flask, jsonify
from flask import session as socket_session
from flask_socketio import disconnect as socketio_disconnect
from flask_socketio import join_room

from app.api.alerts import alerts_bp
from app.api.auth import auth_bp
from app.api.cameras import cameras_bp
from app.api.diagnostics import runtime_bp
from app.api.features import features_bp
from app.api.monitor import monitor_bp
from app.api.prevention import prevention_bp
from app.api.risk import risk_bp
from app.api.scenarios import scenarios_bp
from app.api.status import status_bp
from app.api.stream import stream_bp
from app.cli import register_cli
from app.config import Config
from app.extensions import db, migrate, socketio
from app.models import Alert, User  # noqa: F401  (registra os modelos no metadata)
from app.services.feature_manager import FeatureManager
from app.services.monitor_service import CameraNotFoundError, MonitorService
from app.services.scenario_service import ScenarioService
from app.utils.auth import current_user
from app.utils.connections import user_room
from app.utils.logging_config import configure_logging
from app.utils.salas import salas_do_usuario


def _validar_secret_key(app: Flask) -> None:
    """A sessão de login é assinada com SECRET_KEY.

    Uma chave que esteja no repositório — ou curta demais — deixa qualquer
    pessoa forjar o cookie de um supervisor sem nenhuma credencial. Por isso
    aqui é ERRO de inicialização, não aviso.

    Checar apenas o default do `config.py` não bastava: o `.env.example`, que o
    README manda copiar, entrega `SECRET_KEY=change-me`. Seguindo o passo a
    passo documentado, a aplicação subia com chave pública. Agora a checagem
    olha o CONJUNTO de literais que vivem no repositório, e também o tamanho.
    """
    if not app.config.get("AUTH_REQUIRED", True):
        return

    chave = app.config.get("SECRET_KEY") or ""
    publicas = app.config.get("SECRET_KEYS_PUBLICAS", frozenset())
    minimo = int(app.config.get("SECRET_KEY_MIN_LENGTH", 32))

    if chave in publicas:
        problema = "está num valor que consta no próprio repositório"
    elif len(chave) < minimo:
        problema = f"tem menos de {minimo} caracteres"
    else:
        return

    if not app.config.get("TESTING") and not app.config.get("DEBUG"):
        raise RuntimeError(
            f"SECRET_KEY {problema}. Com autenticação ligada, isso permite forjar a sessão "
            "de qualquer usuário sem credencial. Gere uma chave real e ponha no .env:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"'
        )
    logging.getLogger(__name__).warning(
        "insecure_secret_key",
        extra={"problema": problema, "hint": "aceitável só em desenvolvimento"},
    )


def create_app(config_class: type[Config] = Config) -> Flask:
    configure_logging()

    app = Flask(__name__, static_folder="static")
    app.config.from_object(config_class)
    if not app.config.get("MAX_CONTENT_LENGTH"):
        app.config["MAX_CONTENT_LENGTH"] = 130 * 1024 * 1024

    _validar_secret_key(app)
    if app.config.get("AUTH_REQUIRED", True) and not app.config.get("SESSION_COOKIE_SECURE") and not app.config.get("DEBUG"):
        logging.getLogger(__name__).warning(
            "session_cookie_not_secure",
            extra={"hint": "Atrás de HTTPS, defina SESSION_COOKIE_SECURE=true no .env."},
        )

    db.init_app(app)
    migrate.init_app(app, db)
    socketio.init_app(
        app,
        cors_allowed_origins=app.config.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "*"),
        async_mode=app.config.get("SOCKETIO_ASYNC_MODE", "threading"),
        logger=False,
        engineio_logger=False,
    )

    feature_manager = FeatureManager.from_config(app.config)
    monitor_service = MonitorService(app, socketio, feature_manager)

    app.extensions["feature_manager"] = feature_manager
    app.extensions["monitor_service"] = monitor_service
    app.extensions["scenario_service"] = ScenarioService(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(status_bp)
    app.register_blueprint(alerts_bp)
    app.register_blueprint(cameras_bp)
    app.register_blueprint(monitor_bp)
    app.register_blueprint(features_bp)
    app.register_blueprint(stream_bp)
    app.register_blueprint(runtime_bp)
    app.register_blueprint(risk_bp)
    app.register_blueprint(prevention_bp)
    app.register_blueprint(scenarios_bp)

    # Cobre toda rota LEGADA que opera sobre "a câmera padrão" (/start,
    # /stop, /status, /preflight, /settings, /overlay, /risk-area,
    # /analysis/latest) quando não existe nenhuma câmera cadastrada ainda —
    # em vez de deixar cada rota individual lidar com isso, um handler só.
    # (/video_feed é streaming e trata isso dentro do próprio generator,
    # ver app/api/stream.py — um erro aqui não alcançaria o cliente depois
    # que a resposta já começou a ser enviada.)
    @app.errorhandler(CameraNotFoundError)
    def _handle_no_camera(exc: CameraNotFoundError):
        return jsonify({"error": str(exc), "hint": "Cadastre e inicie uma câmera em /api/cameras antes de usar esta rota."}), 404

    # WebSocket tambem exige sessao: sem isto, todo o feed de analise, alertas
    # e compliance vazaria pra qualquer um que abrisse um socket na porta —
    # protegendo so o REST, a porta dos fundos ficaria escancarada.
    @socketio.on("connect")
    def _authorize_socket(auth=None):  # noqa: ARG001  (assinatura do Flask-SocketIO)
        if not app.config.get("AUTH_REQUIRED", True):
            for sala in salas_do_usuario(None):
                join_room(sala)
            return True
        usuario = current_user()
        if usuario is None:
            logging.getLogger(__name__).warning("socket_rejected_unauthenticated")
            return False
        # Guarda a identidade NA sessao do socket pra poder revalidar depois.
        # Só checar no connect deixava o feed ao vivo (vídeo, alertas, pessoas
        # detectadas) chegando a quem foi desativado ou trocou a senha.
        socket_session["user_id"] = usuario.id
        socket_session["epoch"] = usuario.session_epoch
        join_room(user_room(usuario.id))
        # O escopo de câmera do Operador precisa valer aqui também. Enquanto
        # todo emit era broadcast, ele recebia o feed inteiro do parque pelo
        # socket mesmo levando 404 nas rotas REST das outras áreas.
        # Operador sem setor não entra em sala nenhuma, de propósito.
        for sala in salas_do_usuario(usuario):
            join_room(sala)
        return True

    @socketio.on("revalidate")
    def _revalidate_socket():
        """O cliente chama isto periodicamente; se a sessao morreu, cai fora.

        Um socket aberto vive horas. Sem revalidação, revogar acesso não teria
        efeito nenhum sobre quem já estava conectado.
        """
        if not app.config.get("AUTH_REQUIRED", True):
            return
        if not _socket_ainda_vale():
            logging.getLogger(__name__).info("socket_revoked")
            socketio_disconnect()

    def _socket_ainda_vale() -> bool:
        user_id = socket_session.get("user_id")
        if user_id is None:
            return False
        usuario = db.session.get(User, user_id)
        return bool(
            usuario is not None
            and usuario.active
            and usuario.session_epoch == socket_session.get("epoch")
        )

    register_cli(app)

    with app.app_context():
        if app.config.get("AUTO_CREATE_TABLES", True):
            db.create_all()
        # FORA do guard de AUTO_CREATE_TABLES, e de propósito: criar tabela e
        # carregar os workers das câmeras que já estão no banco são coisas sem
        # relação. Enquanto estavam juntos, quem seguia a recomendação da
        # docs/AMBIENTE.md — Alembic dono do esquema, AUTO_CREATE_TABLES=false
        # — subia com o dashboard morto: as câmeras apareciam na lista, mas
        # `/api/cameras/<id>/start` devolvia 409 e `/start` devolvia 404,
        # porque não havia worker nem câmera padrão.
        #
        # Sem seed automático de propósito — o usuário cadastra as câmeras
        # dele pela tela/API, nada fictício nasce sozinho.
        try:
            monitor_service.load_cameras_from_db()
            app.extensions["scenario_service"].recover_interrupted()
        except Exception as exc:  # noqa: BLE001
            # Banco ainda sem a tabela `cameras`. É o caso NORMAL de
            # `flask --app wsgi db upgrade`, que constrói a aplicação ANTES de
            # rodar a migração. Falhar aqui mataria justamente o comando de
            # onboarding — o espelho do desvio #1 da AMBIENTE.md.
            #
            # Só o NOME da exceção vai para o log: a mensagem pode carregar a
            # URL da fonte de uma câmera, e com ela a credencial.
            logging.getLogger(__name__).info(
                "workers_nao_carregados_no_boot",
                extra={"motivo": type(exc).__name__, "hint": "esquema ainda não migrado?"},
            )

    return app
