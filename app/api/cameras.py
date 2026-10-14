from __future__ import annotations

import time

import cv2
from flask import Blueprint, Response, current_app, jsonify, request

from app.api.placeholder import placeholder_jpeg
from app.extensions import db
from app.llm import MARCA_DE_REDACAO
from app.models import DEFAULT_CAMERA_FEATURES, ROLE_OPERATOR, ROLE_TECHNICAL, Camera
from app.services.gate_service import normalizar_exigidos
from app.utils.auth import camera_permitida, camera_scope, erro_fora_do_escopo, login_required, require_role
from app.utils.stream_access import stream_access_check
from app.vision.video_stream import capture_api

cameras_bp = Blueprint("cameras", __name__)

VALID_SOURCE_TYPES = {"USB", "RTSP", "Arquivo"}
# Mesmas 8 chaves de FeatureManager.AVAILABLE_FEATURES — validado aqui pra
# nunca deixar entrar uma chave de feature que o resto do sistema (Sidebar,
# RuleEngine) não reconhece.
VALID_FEATURE_KEYS = set(DEFAULT_CAMERA_FEATURES.keys())


def _sem_setor() -> bool:
    """Operador logado mas sem camera atribuida."""
    from app.utils.auth import _operador_sem_setor

    return _operador_sem_setor()


def _validation_error(message: str):
    return jsonify({"error": message}), 400


def _validate_and_apply(camera: Camera, payload: dict, *, is_create: bool) -> tuple[str | None, int]:
    """Aplica o payload em `camera` (in-place) se válido. Retorna (erro, status)
    — (None, 200) se tudo certo. Compartilhado entre create/update pra não
    duplicar as mesmas regras em dois lugares e um dia divergir."""
    if not isinstance(payload, dict):
        return "Payload inválido — esperado um objeto JSON.", 400

    if is_create or "name" in payload:
        name = str(payload.get("name", "")).strip()
        if not name:
            return "'name' é obrigatório.", 400
        camera.name = name[:120]

    if "location" in payload:
        location = payload.get("location")
        camera.location = str(location).strip()[:160] if location else None

    if is_create or "source_type" in payload:
        source_type = str(payload.get("source_type", camera.source_type or "USB")).strip()
        if source_type not in VALID_SOURCE_TYPES:
            return f"'source_type' deve ser um de {sorted(VALID_SOURCE_TYPES)}.", 400
        camera.source_type = source_type

    if is_create or "source" in payload:
        source = str(payload.get("source", "")).strip()
        if not source:
            return "'source' é obrigatório (índice USB, URL RTSP ou caminho de arquivo).", 400
        # A API DEVOLVE a URL com a credencial redigida (Camera.to_dict), e o
        # formulario do frontend e preenchido com esse valor. Salvar sem editar
        # mandaria `rtsp://***@host` de volta; sobrescrever a credencial real
        # por isso mataria a camera com a causa invisivel na tela. Valor
        # redigido chegando num UPDATE significa "nao mexi na fonte".
        if MARCA_DE_REDACAO in source:
            if is_create:
                return "'source' contém a marca de redação (***). Informe a URL real.", 400
        else:
            camera.source = source[:255]

    if "fps" in payload:
        try:
            fps = int(payload["fps"])
        except (TypeError, ValueError):
            return "'fps' deve ser um número inteiro.", 400
        camera.fps = max(1, min(60, fps))

    if "width" in payload:
        try:
            camera.width = max(160, min(3840, int(payload["width"])))
        except (TypeError, ValueError):
            return "'width' deve ser um número inteiro.", 400

    if "height" in payload:
        try:
            camera.height = max(120, min(2160, int(payload["height"])))
        except (TypeError, ValueError):
            return "'height' deve ser um número inteiro.", 400

    if "rotation" in payload:
        try:
            rotation = int(payload["rotation"])
        except (TypeError, ValueError):
            return "'rotation' deve ser um número inteiro.", 400
        if rotation not in (0, 90, 180, 270):
            return "'rotation' deve ser um de [0, 90, 180, 270].", 400
        camera.rotation = rotation

    if "enabled" in payload:
        camera.enabled = bool(payload["enabled"])

    if "features" in payload:
        features = payload["features"]
        if not isinstance(features, dict):
            return "'features' deve ser um objeto {chave: true/false}.", 400
        unknown = set(features.keys()) - VALID_FEATURE_KEYS
        if unknown:
            return f"Features desconhecidas: {sorted(unknown)}. Válidas: {sorted(VALID_FEATURE_KEYS)}.", 400
        current = dict(camera.features_json or DEFAULT_CAMERA_FEATURES)
        current.update({key: bool(value) for key, value in features.items()})
        camera.features_json = current

    if is_create and not camera.features_json:
        camera.features_json = dict(DEFAULT_CAMERA_FEATURES)

    return None, 200


@cameras_bp.get("/api/cameras/discover")
@require_role(ROLE_TECHNICAL)
def discover_cameras():
    """Testa de verdade quais índices USB respondem AGORA (Fase A, Passo 6
    — ver conversa: 'as câmeras que são detectadas apareçam na parte de
    configuração'). Abre e fecha cada índice rapidamente, só pra confirmar
    que existe hardware ali — não fica com a câmera presa depois.

    Só cobre índices numéricos (USB/webcam). RTSP e arquivo continuam
    exigindo endereço digitado manualmente — não tem como "descobrir"
    uma câmera de rede sem o operador saber o IP dela.
    """
    try:
        max_index = min(int(request.args.get("max_index", 5)), 10)
    except (TypeError, ValueError):
        max_index = 5

    already_registered = {
        c.source: c.name for c in Camera.query.filter_by(source_type="USB").all()
    }

    results = []
    for index in range(max_index + 1):
        cap = cv2.VideoCapture(index, capture_api(index))
        available = bool(cap.isOpened())
        width = height = None
        if available:
            ok, frame = cap.read()
            if ok and frame is not None:
                height, width = frame.shape[:2]
            else:
                available = False  # abre mas não entrega frame — trata como indisponível
        cap.release()
        results.append({
            "index": index,
            "source": str(index),
            "available": available,
            "width": width,
            "height": height,
            "already_registered": str(index) in already_registered,
            "registered_as": already_registered.get(str(index)),
        })

    return jsonify({"items": results, "count": len(results)})


@cameras_bp.get("/api/cameras")
@login_required
def list_cameras():
    query = Camera.query
    escopo = camera_scope()
    if escopo is not None:
        query = query.filter(Camera.id == escopo)
    elif _sem_setor():
        # Operador ainda sem setor: lista vazia, nao o parque inteiro.
        return jsonify({"items": [], "count": 0})
    cameras = query.order_by(Camera.id.asc()).all()
    return jsonify({"items": [c.to_dict() for c in cameras], "count": len(cameras)})


@cameras_bp.get("/api/cameras/<int:camera_id>")
@login_required
def get_camera(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    return jsonify(camera.to_dict())


@cameras_bp.post("/api/cameras")
@require_role(ROLE_TECHNICAL)
def create_camera():
    payload = request.get_json(silent=True) or {}
    camera = Camera(features_json=dict(DEFAULT_CAMERA_FEATURES))
    error, status = _validate_and_apply(camera, payload, is_create=True)
    if error:
        return _validation_error(error)
    db.session.add(camera)
    db.session.commit()
    _emit_cameras_updated()
    return jsonify(camera.to_dict()), 201


@cameras_bp.put("/api/cameras/<int:camera_id>")
@cameras_bp.patch("/api/cameras/<int:camera_id>")
@require_role(ROLE_TECHNICAL)
def update_camera(camera_id: int):
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    payload = request.get_json(silent=True) or {}
    error, status = _validate_and_apply(camera, payload, is_create=False)
    if error:
        return _validation_error(error)
    db.session.commit()
    _emit_cameras_updated()
    return jsonify(camera.to_dict())


@cameras_bp.delete("/api/cameras/<int:camera_id>")
@require_role(ROLE_TECHNICAL)
def delete_camera(camera_id: int):
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    # Sem trava de "última câmera" de propósito — zero câmeras cadastradas
    # é um estado válido agora (sem seed automático, ver conversa: "tira
    # todas as câmeras fakes, deixa que eu cadastro"). Frontend e rotas
    # legadas já tratam esse estado graciosamente (ver App.tsx e o
    # errorhandler de CameraNotFoundError em app/__init__.py).
    db.session.delete(camera)
    db.session.commit()
    _emit_cameras_updated()
    return jsonify({"deleted": True, "id": camera_id})


# ---- controle por câmera (Fase A, Passo 5) --------------------------------
# As rotas legadas (/start, /stop, /status, /video_feed em app/api/monitor.py
# e stream.py) continuam existindo e operando sobre "a câmera padrão" — sem
# mudança nenhuma pra quem só usa 1 câmera. Estas aqui são NOVAS, escopadas
# por camera_id, é o que a tela de "Configurar câmera" do frontend vai usar
# pra ligar/desligar uma câmera específica em vez da global.
@cameras_bp.get("/api/cameras/<int:camera_id>/status")
@login_required
def camera_status(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify(monitor.status(camera_id=camera_id))
    except LookupError:
        # Câmera existe no banco mas não tem worker ativo (ex.: enabled=False,
        # ou criada há pouco e o reload ainda não rodou).
        return jsonify({"running": False, "error": "câmera sem worker ativo — verifique se está habilitada"}), 409


@cameras_bp.post("/api/cameras/<int:camera_id>/start")
@require_role(ROLE_OPERATOR)
def camera_start(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify(monitor.start(camera_id=camera_id))
    except LookupError:
        return jsonify({"error": "câmera sem worker ativo — verifique se está habilitada"}), 409


@cameras_bp.post("/api/cameras/<int:camera_id>/stop")
@require_role(ROLE_OPERATOR)
def camera_stop(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify(monitor.stop(camera_id=camera_id))
    except LookupError:
        return jsonify({"error": "câmera sem worker ativo — verifique se está habilitada"}), 409


@cameras_bp.get("/api/cameras/<int:camera_id>/video_feed")
@login_required
def camera_video_feed(camera_id: int):
    # O feed e o dado mais sensivel do sistema: imagem de pessoas trabalhando.
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    target_fps = max(1, int(camera.fps or 12))
    access_allowed = stream_access_check(camera_id)

    def generate():
        # Manda cada frame UMA vez, esperando aparecer um novo em vez de
        # reenviar o último num relógio próprio. Os dois relógios (o do worker
        # e o deste gerador) nunca estiveram em fase, então antes o mesmo
        # quadro saía repetido e outros eram pulados — no navegador isso é
        # engasgo, e ainda inflava qualquer contagem de quadros feita no
        # cliente, mascarando o problema.
        espera = 1 / (target_fps * 4)  # amostra mais fino que a taxa de frames
        limite_ocioso = max(1.0, 2 / target_fps)  # sem frame novo? reenvia pra manter a conexão viva
        ultima_versao = -1
        ultimo_envio = time.monotonic()
        while True:
            if not access_allowed():
                return
            try:
                jpeg, versao = monitor.latest_jpeg_versionado(camera_id=camera_id)
            except LookupError:
                jpeg, versao = None, -1

            agora = time.monotonic()
            novo = versao != ultima_versao
            if not novo and agora - ultimo_envio < limite_ocioso:
                time.sleep(espera)
                continue

            ultima_versao = versao
            ultimo_envio = agora
            body = jpeg or placeholder_jpeg(f"Camera {camera_id}: parada ou sem frame")
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + body + b"\r\n"

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@cameras_bp.get("/api/cameras/<int:camera_id>/frame.jpg")
@login_required
def camera_frame(camera_id: int):
    """UM quadro e a conexao fecha. E o que o painel usa nas miniaturas:
    o video continuo (MJPEG) de cada camera prendia uma das 6 conexoes que o
    navegador permite por site, e com 6 cameras ligadas os cliques (portaria,
    recursos...) ficavam na fila ate um F5."""
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        jpeg, _versao = monitor.latest_jpeg_versionado(camera_id=camera_id)
    except LookupError:
        jpeg = None
    resposta = Response(jpeg or placeholder_jpeg(f"Camera {camera_id}: parada ou sem frame"), mimetype="image/jpeg")
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@cameras_bp.get("/api/cameras/<int:camera_id>/analysis")
@login_required
def camera_analysis(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify(monitor.latest_analysis(camera_id=camera_id) or {})
    except LookupError:
        return jsonify({})


def _emit_cameras_updated() -> None:
    """Avisa quem estiver conectado (frontend) que a lista de câmeras
    mudou — mesmo padrão que /features já usa (features_updated). Sem
    payload pesado; quem receber decide se re-busca a lista via GET.

    Também recarrega os workers do MonitorService (Fase A, Passo 4) — uma
    câmera criada/editada/removida via este CRUD já reflete no motor de
    captura sem precisar reiniciar o servidor."""
    monitor = current_app.extensions.get("monitor_service")
    if monitor is not None:
        monitor.socketio.emit("cameras_updated", {})
        if hasattr(monitor, "load_cameras_from_db"):
            monitor.load_cameras_from_db()


@cameras_bp.get("/api/cameras/<int:camera_id>/gate")
@login_required
def get_camera_gate(camera_id: int):
    """Configuração + veredito atual da portaria. Câmera parada: 'waiting'."""
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    camera = db.session.get(Camera, camera_id)
    if camera is None:
        return jsonify({"error": "câmera não encontrada"}), 404
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify(monitor.gate_state(camera_id))
    except LookupError:
        exigidos = camera.gate_required
        return jsonify(
            {
                "camera_id": camera_id,
                "enabled": exigidos is not None,
                "required": exigidos or [],
                "verdict": "waiting" if exigidos else "off",
                "missing": [],
                "people": 0,
            }
        )


@cameras_bp.put("/api/cameras/<int:camera_id>/gate")
@require_role(ROLE_TECHNICAL)
def put_camera_gate(camera_id: int):
    """Body: {"required": ["helmet","vest"]} liga; {"required": null} desliga."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or "required" not in payload:
        return _validation_error('Envie {"required": [...]} ou {"required": null}.')
    try:
        exigidos = normalizar_exigidos(payload["required"])
        monitor = current_app.extensions["monitor_service"]
        return jsonify(monitor.update_gate(camera_id, exigidos))
    except ValueError as exc:
        return _validation_error(str(exc))
    except LookupError:
        return jsonify({"error": "câmera não encontrada"}), 404


@cameras_bp.get("/api/cameras/<int:camera_id>/risk-area")
@login_required
def get_camera_risk_area(camera_id: int):
    if not camera_permitida(camera_id):
        return erro_fora_do_escopo()
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify({"risk_area": monitor.risk_area_state(camera_id=camera_id)})
    except LookupError:
        return jsonify({"error": "câmera sem worker ativo — verifique se está habilitada"}), 409


@cameras_bp.put("/api/cameras/<int:camera_id>/risk-area")
@cameras_bp.patch("/api/cameras/<int:camera_id>/risk-area")
@require_role(ROLE_TECHNICAL)
def put_camera_risk_area(camera_id: int):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _validation_error("Payload inválido — esperado um objeto JSON.")
    monitor = current_app.extensions["monitor_service"]
    try:
        return jsonify({"risk_area": monitor.update_risk_area(payload, camera_id=camera_id)})
    except ValueError as exc:
        return _validation_error(str(exc))
    except LookupError:
        return jsonify({"error": "câmera sem worker ativo — verifique se está habilitada"}), 409
