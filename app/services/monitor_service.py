from __future__ import annotations

import threading
from typing import Any

from flask import Flask
from flask_socketio import SocketIO

from app.config import Config
from app.extensions import db
from app.models import DEFAULT_CAMERA_FEATURES, Camera
from app.services.camera_worker import CameraWorker
from app.services.feature_manager import FeatureManager
from app.services.llm_risk_service import ServicoDeRiscoLLM
from app.services.prevention_service import validate_config
from app.vision.ensemble_ppe_detector import EnsemblePPEDetector
from app.vision.pose_estimator import MediaPipePoseEstimator
from app.vision.yolo_ppe_detector import YoloPPEDetector


class CameraNotFoundError(LookupError):
    pass


class MonitorService:
    """Gerenciador de câmeras — Fase A, Passo 4.

    Carrega os modelos YOLO/pose UMA ÚNICA VEZ (compartilhados entre todos
    os workers, ver docstring de CameraWorker) e mantém um CameraWorker por
    câmera cadastrada no banco (app.models.Camera, enabled=True).

    Compatibilidade com rotas antigas: todo método de instância aceita
    `camera_id: int | None = None`. Quando None, opera sobre a "câmera
    padrão" (a de menor id, tipicamente a semeada no Passo 1) — é assim que
    /start, /stop, /status etc continuam funcionando sem mudar nem uma
    linha em app/api/*.py. camera_id explícito só passa a ser usado pelas
    rotas a partir do Passo 5.

    Regras e rastreamento são isolados por câmera. O lock dos modelos
    compartilhados garante chamadas seguras, mas também serializa inferências;
    a espera deve ser medida com as cinco câmeras em execução.
    """

    def __init__(self, app: Flask, socketio: SocketIO, feature_manager: FeatureManager) -> None:
        self.app = app
        self.socketio = socketio
        self.feature_manager = feature_manager  # segue controlando a câmera padrão via /features, como sempre

        # ---- modelos compartilhados: criados 1x, injetados em cada worker ----
        detector_principal = YoloPPEDetector(
            model_path=app.config.get("PPE_MODEL_PATH", "yolov8n.pt"),
            confidence=app.config.get("YOLO_CONFIDENCE", 0.35),
            device=app.config.get("YOLO_DEVICE"),
            classes=CameraWorker._parse_yolo_classes(app.config.get("YOLO_CLASSES", "")),
            max_detections=app.config.get("YOLO_MAX_DETECTIONS", 100),
            require_person=False,
            imgsz=app.config.get("YOLO_IMGSZ", 640),
            half=app.config.get("YOLO_HALF", True),
        )
        # PPE_EXTRA_MODELS vazio => self.detector e o detector unico de sempre.
        # Com um ou mais caminhos, envelopa tudo num ensemble que roda todos no
        # mesmo frame e funde o resultado (ver EnsemblePPEDetector). Cada extra
        # roda SEM YOLO_CLASSES: aquele filtro sao indices do Vyra.
        extras_raw = str(app.config.get("PPE_EXTRA_MODELS", "") or "")
        extra_paths = [p.strip() for p in extras_raw.split(",") if p.strip()]
        if extra_paths:
            extra_imgsz = app.config.get("PPE_EXTRA_MODELS_IMGSZ", 0) or app.config.get("YOLO_IMGSZ", 640)
            # Classes que NENHUM modelo extra deve contribuir — confirmado por
            # diagnostico manual: o epi_pretrained.pt (nano, dataset pequeno)
            # deu "Gloves" 0.78 numa mao NUA levantada (o Vyra, no mesmo frame,
            # se absteve certo). Falso positivo de luva e o pior tipo de erro
            # aqui: o sistema marca a pessoa como protegida quando nao esta.
            # Em vez de confiar no piso de confianca (a deteccao errada saiu
            # com 0.78, acima de qualquer piso razoavel), a classe some da
            # saida do(s) extra(s) e fica so a cargo do Vyra.
            excluir_raw = str(app.config.get("PPE_EXTRA_MODELS_EXCLUDE_LABELS", "") or "")
            excluir_labels = {item.strip() for item in excluir_raw.split(",") if item.strip()}
            extras = []
            for path in extra_paths:
                extra = YoloPPEDetector(
                    model_path=path,
                    confidence=app.config.get("YOLO_CONFIDENCE", 0.35),
                    device=app.config.get("YOLO_DEVICE"),
                    classes=None,
                    max_detections=app.config.get("YOLO_MAX_DETECTIONS", 100),
                    require_person=False,
                    imgsz=extra_imgsz,
                    augment=app.config.get("PPE_EXTRA_MODELS_AUGMENT", True),
                    half=app.config.get("YOLO_HALF", True),
                )
                if excluir_labels:
                    # Forca o load aqui (1x, no boot) pra traduzir label -> id
                    # DESTE peso especifico antes do primeiro detect().
                    nomes = extra.model.names
                    mantidos = [
                        class_id
                        for class_id, raw_nome in nomes.items()
                        if YoloPPEDetector.normalize_label(str(raw_nome)) not in excluir_labels
                    ]
                    extra.classes = mantidos or None
                extras.append(extra)
            self.detector = EnsemblePPEDetector([detector_principal, *extras])
        else:
            self.detector = detector_principal
        # Segundo YOLO (COCO, classe 0) usado APENAS quando
        # MULTI_PERSON_DETECTION=true — isto e, quando PPE_MODEL_PATH aponta pra
        # um modelo de EPI sem classe "person" propria (ex: epi_pretrained.pt).
        # Com o Vyra, que ja detecta "Person", fica ocioso.
        # Mesmo par de modelos, compartilhado por todas as cameras.
        self.person_detector = YoloPPEDetector(
            model_path=app.config.get("PERSON_MODEL_PATH", "yolov8n.pt"),
            confidence=app.config.get("YOLO_CONFIDENCE", 0.35),
            device=app.config.get("YOLO_DEVICE"),
            classes=[0],
            max_detections=app.config.get("YOLO_MAX_DETECTIONS", 100),
            imgsz=app.config.get("YOLO_IMGSZ", 640),
            half=app.config.get("YOLO_HALF", True),
        )
        self.pose_estimator = MediaPipePoseEstimator(
            min_detection_confidence=app.config.get("POSE_MIN_DETECTION_CONFIDENCE", 0.5),
            min_tracking_confidence=app.config.get("POSE_MIN_TRACKING_CONFIDENCE", 0.5),
        )
        # Serializa chamadas de inferência entre workers (ver CameraWorker).
        self._inference_lock = threading.Lock()

        # ---- camada LLM: um servico POR camera ---------------------------
        # Um por camera, e nao um compartilhado, porque `ao_concluir` e um
        # atributo unico do servico e nao recebe camera_id: com uma instancia
        # so, o callback de uma camera sobrescreveria o da outra e a segunda
        # opiniao apareceria na camera errada. Custa quase nada — o cliente do
        # Gemini nasce sob demanda (ProvedorGemini nao cria cliente no
        # __init__), entao uma instancia por camera e um punhado de atributos,
        # sem rede e sem SDK carregado.
        # O debounce e o "uma em voo" ja sao por camera no desenho do servico,
        # entao a semantica nao muda.
        self._llm_habilitado = bool(app.config.get("LLM_ENABLED", False))

        self._workers: dict[int, CameraWorker] = {}
        self._workers_lock = threading.RLock()
        self._default_camera_id: int | None = None

    # ---- ciclo de vida dos workers -----------------------------------------
    def load_cameras_from_db(self) -> None:
        """Sincroniza self._workers com a tabela `cameras`. Chamado no boot
        (app/__init__.py, dentro do app_context) e de novo sempre que o CRUD
        de câmeras cria/edita/remove uma linha — assim uma câmera nova já
        ganha worker, e uma câmera EDITADA (fonte/fps/resolução) recebe um
        worker novo com a config atualizada, sem precisar reiniciar o
        servidor. Se a câmera estava rodando antes da edição, o worker novo
        já sobe rodando também — senão a edição pareceria salvar mas não
        mudar nada de verdade até o próximo restart."""
        with self._workers_lock:
            db_cameras = Camera.query.filter_by(enabled=True).order_by(Camera.id.asc()).all()
            db_camera_ids = {c.id for c in db_cameras}
            default_id = db_cameras[0].id if db_cameras else None

            # remove workers de câmeras que sumiram ou foram desabilitadas
            for camera_id in list(self._workers.keys()):
                if camera_id not in db_camera_ids:
                    self._stop_and_discard_worker(camera_id)

            for camera in db_cameras:
                existing = self._workers.get(camera.id)
                if existing is None:
                    # câmera nova — cria worker do zero, ainda parado
                    self._workers[camera.id] = self._build_worker(camera)
                    continue
                if self._worker_config_changed(existing, camera):
                    was_running = existing.status().get("running", False)
                    self._stop_and_discard_worker(camera.id)
                    new_worker = self._build_worker(camera)
                    self._workers[camera.id] = new_worker
                    if was_running:
                        new_worker.start()
                else:
                    if existing.prevention_service.config != validate_config(camera.prevention_json or {}):
                        existing.prevention_service.configure(camera.prevention_json or {})
                        existing._prevention_alerts = []
                        existing._cached_analysis = None
                    features = self._camera_features(camera)
                    if existing.features_configuradas != features:
                        existing.feature_manager.update(features)
                        existing.features_configuradas = features
                        existing._cached_analysis = None
                        existing._detect_counter = 0

            self._default_camera_id = default_id
            if default_id is not None:
                self.feature_manager = self._workers[default_id].feature_manager
                self.app.extensions["feature_manager"] = self.feature_manager

    @staticmethod
    def _worker_config_changed(worker: CameraWorker, camera: Camera) -> bool:
        # `fonte_configurada`, e NAO `video_stream.source`: em modo fixture o
        # worker troca a fonte de video em runtime (ver
        # CameraWorker._assumir_fonte_reserva). Comparando com a fonte trocada,
        # todo `load_cameras_from_db()` — que roda a cada CRUD de camera — veria
        # "a fonte mudou", reconstruiria o worker e derrubaria o modo fixture no
        # meio do demo, com a camera voltando a tentar a fonte morta do zero.
        current_source = str(worker.fonte_configurada)
        new_source = str(Config.parse_video_source(camera.source))
        return (
            current_source != new_source
            or worker.fps_configurado != camera.fps
            or worker.video_stream.width != camera.width
            or worker.video_stream.height != camera.height
            or worker.rotation != int(camera.rotation or 0)
        )

    @staticmethod
    def _camera_features(camera: Camera) -> dict[str, bool]:
        return {**DEFAULT_CAMERA_FEATURES, **(camera.features_json or {})}

    def _build_worker(self, camera: Camera) -> CameraWorker:
        # Todas leem as regras persistidas; /features acompanha a padrão.
        features = self._camera_features(camera)
        feature_manager = FeatureManager.from_camera_features(features)
        worker = CameraWorker(
            self.app,
            self.socketio,
            feature_manager,
            camera_id=camera.id,
            source=Config.parse_video_source(camera.source),
            fps=camera.fps,
            width=camera.width,
            height=camera.height,
            rotation=int(camera.rotation or 0),
            risk_polygon=camera.risk_polygon,
            gate_required=camera.gate_required,
            detector=self.detector,
            person_detector=self.person_detector,
            pose_estimator=self.pose_estimator,
            inference_lock=self._inference_lock,
            servico_llm=self._criar_servico_llm(),
            prevention_config=camera.prevention_json,
        )
        worker.features_configuradas = features
        return worker

    def _criar_servico_llm(self):
        """Servico de segunda opiniao para UMA camera, ou None.

        `None` quando LLM_ENABLED=false ou nao ha GEMINI_API_KEY — que e o
        default. A fabrica do servico ja resolve isso: sem chave ela devolve um
        servico com `habilitado=False`, e ai nao vale carregar nada.
        """
        if not self._llm_habilitado:
            return None
        servico = ServicoDeRiscoLLM.a_partir_da_config(self.app.config)
        return servico if servico.habilitado else None

    def _stop_and_discard_worker(self, camera_id: int) -> None:
        worker = self._workers.pop(camera_id, None)
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass

    def _get_worker(self, camera_id: int | None) -> CameraWorker:
        target_id = camera_id if camera_id is not None else self._default_camera_id
        with self._workers_lock:
            worker = self._workers.get(target_id) if target_id is not None else None
        if worker is None:
            raise CameraNotFoundError(f"câmera {target_id!r} não encontrada ou sem worker ativo")
        return worker

    def features_of(self, camera_id: int | None = None) -> FeatureManager:
        """FeatureManager que de fato governa a câmera: o global para a padrão,
        o próprio worker para as demais."""
        return self._get_worker(camera_id).feature_manager

    def start_all(self) -> dict[int, dict[str, Any]]:
        with self._workers_lock:
            workers = dict(self._workers)
        return {camera_id: worker.start() for camera_id, worker in workers.items()}

    def stop_all(self) -> dict[int, dict[str, Any]]:
        with self._workers_lock:
            workers = dict(self._workers)
        return {camera_id: worker.stop() for camera_id, worker in workers.items()}

    def status_all(self) -> dict[int, dict[str, Any]]:
        with self._workers_lock:
            workers = dict(self._workers)
        return {camera_id: worker.status() for camera_id, worker in workers.items()}

    # ---- delegação por câmera (camera_id=None => câmera padrão) -----------
    def start(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).start()

    def stop(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).stop()

    def status(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).status()

    def preflight(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).preflight()

    def latest_jpeg(self, camera_id: int | None = None) -> bytes | None:
        return self._get_worker(camera_id).latest_jpeg()

    def latest_jpeg_versionado(self, camera_id: int | None = None) -> tuple[bytes | None, int]:
        return self._get_worker(camera_id).latest_jpeg_versionado()

    def latest_analysis(self, camera_id: int | None = None) -> dict[str, Any] | None:
        return self._get_worker(camera_id).latest_analysis()

    def update_alert_snapshot(self, payload: dict[str, Any]) -> None:
        with self._workers_lock:
            worker = self._workers.get(payload.get("camera_id"))
        if worker is not None:
            worker.alert_state_service.apply_external_update(payload)

    def settings(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).settings()

    def update_settings(self, updates: dict[str, Any], camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).update_settings(updates)

    def get_overlay(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).get_overlay()

    def update_overlay(self, updates: dict[str, Any], camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).update_overlay(updates)

    def limpar_alertas_ativos(self, camera_id: int | None = None) -> dict[str, Any]:
        """Resolve os alertas ativos de UMA camera (ou de todas).

        Camera rodando: pede ao worker, que resolve na propria thread. Camera
        parada: nao ha dono do estado, entao resolve direto no banco.
        """
        from app.repositories.alert_repository import AlertRepository

        with self._workers_lock:
            workers = dict(self._workers)
        alvo_ids = [camera_id] if camera_id is not None else list(workers)
        pedidos: list[int] = []
        direto = 0
        for cid in alvo_ids:
            worker = workers.get(cid)
            if worker is not None and worker._running.is_set():
                worker.pedir_limpeza_alertas()
                pedidos.append(cid)
            else:
                direto += AlertRepository().resolve_all_active(reason="manual_clear", camera_id=cid)
        return {"pedidos": pedidos, "resolvidos_direto": direto}

    def risk_area_state(self, camera_id: int | None = None) -> dict[str, Any]:
        return self._get_worker(camera_id).risk_area_state()

    def update_risk_area(self, payload: dict[str, Any], camera_id: int | None = None) -> dict[str, Any]:
        worker = self._get_worker(camera_id)
        state = worker.update_risk_area(payload)
        # Persiste na câmera: sem isto a zona voltava pro padrão do .env a cada
        # reinício e toda câmera acabava com o mesmo polígono.
        camera = db.session.get(Camera, worker.camera_id)
        if camera is not None:
            camera.risk_polygon = [[p["x"], p["y"]] for p in state["polygon"]]
            db.session.commit()
        return state

    def gate_state(self, camera_id: int) -> dict[str, Any]:
        return self._get_worker(camera_id).gate_state()

    def update_gate(self, camera_id: int, required: list[str] | None) -> dict[str, Any]:
        """Liga/desliga a portaria e persiste (funciona com a câmera parada)."""
        camera = db.session.get(Camera, camera_id)
        if camera is None:
            raise LookupError("câmera não encontrada")
        camera.gate_required = required
        db.session.commit()
        try:
            self._get_worker(camera_id).set_gate(required)
        except LookupError:
            pass  # sem worker ativo: vale no próximo start
        return {"camera_id": camera_id, "required": required or [], "enabled": required is not None}

    # ---- atributos que as rotas acessam direto (sempre a câmera padrão) ---
    @property
    def snapshot_service(self):
        return self._get_worker(None).snapshot_service

    @property
    def alert_state_service(self):
        return self._get_worker(None).alert_state_service

    @property
    def video_stream(self):
        return self._get_worker(None).video_stream
