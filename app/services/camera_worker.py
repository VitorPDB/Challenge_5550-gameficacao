from __future__ import annotations

import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import cv2
from flask import Flask
from flask_socketio import SocketIO
from sqlalchemy import text as sql_text

from app.config import BASE_DIR, Config
from app.extensions import db
from app.llm import redigir_segredos
from app.repositories.alert_repository import AlertRepository
from app.repositories.event_repository import EventRepository
from app.services.alert_state_service import AlertStateService
from app.services.compliance_service import ComplianceService
from app.services.feature_manager import FeatureManager
from app.services.gate_service import GateState, avaliar_quadro
from app.services.prevention_service import PreventionService, draw_prevention
from app.services.risk_rules import RuleEngine, RuleEvaluation
from app.services.risk_score_service import compute_risk_score
from app.services.snapshot_service import SnapshotService
from app.services.storage_cleanup_service import StorageCleanupService
from app.utils.salas import emitir_para_camera
from app.vision.annotator import FrameAnnotator
from app.vision.person_compliance_matcher import PPE_KEYS
from app.vision.person_tracker import PersonTracker
from app.vision.pose_estimator import MediaPipePoseEstimator
from app.vision.schemas import FrameAnalysis, PoseResult
from app.vision.video_stream import VideoStream
from app.vision.yolo_ppe_detector import YoloPPEDetector

logger = logging.getLogger(__name__)

# Janelas do diagnostico de tela. Sao diferentes de proposito: 5 s responde
# "esta travando AGORA" (janela longa demais esconde uma queda recente atras
# da media), e 30 s e o que o campo pediu para a contagem por classe — curto
# demais e a contagem pisca entre 0 e 3 e ninguem consegue ler.
JANELA_FPS_S = 5.0
JANELA_DETECCOES_S = 30.0
# Intervalo entre medicoes de brilho. NAO e por frame: a 15 fps seriam 15
# medicoes por segundo dentro do loop de captura, e o orcamento desse loop e o
# que este projeto passa o tempo defendendo. Uma vez por segundo e de sobra
# para uma janela de 5 s, e o custo vira irrelevante.
INTERVALO_AMOSTRA_BRILHO_S = 1.0
# Amostras por lado usadas por `brilho_do_frame`: 32 => ~1.000 pixels lidos,
# independente da resolucao da fonte.
AMOSTRAS_DE_BRILHO_POR_LADO = 32


def brilho_do_frame(frame) -> float:
    """Brilho medio APROXIMADO, por subamostragem.

    Ler os 921.600 valores de um frame 640x480x3 a cada medicao seria caro sem
    necessidade: a pergunta e "esta imagem esta zerada?", e para isso uma grade
    esparsa responde igual. O passo se adapta a resolucao, entao o custo nao
    cresce com fonte maior.

    `float(...)` explicito porque `numpy.mean` devolve `np.float64`, e este
    numero vai para o payload JSON do dashboard.
    """
    lado_menor = min(frame.shape[0], frame.shape[1])
    passo = max(1, lado_menor // AMOSTRAS_DE_BRILHO_POR_LADO)
    return float(frame[::passo, ::passo].mean())


class CameraWorker:
    """Captura + análise + estado de UMA câmera.

    Fase A, Passo 4: os modelos YOLO/pose (detector/person_detector/
    pose_estimator) e o `inference_lock` agora são INJETADOS pelo
    MonitorService, não mais criados aqui — carregados uma única vez e
    compartilhados entre todos os workers, pra não multiplicar VRAM por
    câmera. `source`, `fps` e `feature_manager` também passam a ser
    parâmetros por-câmera (vindos de app.models.Camera) em vez de lidos
    direto do app.config global — cada câmera tem sua própria fonte de
    vídeo e seu próprio conjunto de features ligadas.

    O `inference_lock` serializa as chamadas de inferência entre workers
    (um por vez usa a GPU pros modelos compartilhados) — captura de frame
    de cada câmera continua paralela, só a etapa de "rodar o modelo em
    cima do frame" é serializada. Numa GPU só, isso é seguro por natureza
    (ela já processa um kernel por vez) e evita qualquer risco de dois
    threads chamando forward() no mesmo objeto de modelo ao mesmo tempo.
    """

    def __init__(
        self,
        app: Flask,
        socketio: SocketIO,
        feature_manager: FeatureManager,
        *,
        camera_id: int | None,
        source: str | int,
        fps: int,
        width: int = 960,
        height: int = 540,
        rotation: int = 0,
        risk_polygon: list | None = None,
        gate_required: list | None = None,
        detector: YoloPPEDetector,
        person_detector: YoloPPEDetector,
        pose_estimator: MediaPipePoseEstimator,
        inference_lock: threading.Lock,
        servico_llm: Any | None = None,
        prevention_config: dict | None = None,
    ) -> None:
        self.app = app
        self.socketio = socketio
        self.feature_manager = feature_manager
        self.camera_id = camera_id
        self.prevention_service = PreventionService(prevention_config)
        self._prevention_alerts = []
        self.target_fps = max(1, int(fps))
        self.fps_configurado = self.target_fps
        # So um destes 4 valores tem sentido pra `cv2.rotate` — qualquer outro
        # vira 0 (sem rotacao) em vez de derrubar o worker.
        self.rotation = int(rotation) if int(rotation) in (0, 90, 180, 270) else 0
        self._running = threading.Event()
        # Pedido de "limpar alertas ativos" vindo da rota HTTP. Quem executa e o
        # proprio loop do worker: o estado dos alertas e os objetos do ORM sao
        # dele, e mexer neles de outra thread ja causou erro de sessao.
        self._limpeza_alertas_pedida = threading.Event()
        self._thread_lock = threading.RLock()
        self._task = None
        self._latest_jpeg: bytes | None = None
        self._latest_analysis: dict[str, Any] | None = None
        self._last_error: str | None = None
        # Ultimo estado de captura JA comunicado — so emite evento na transicao,
        # nao a cada frame ruim (a 12 FPS seriam 12 eventos por segundo).
        self._last_stream_state: str | None = None
        self._frame_counter = 0
        self._last_frame_at: float | None = None
        self._last_risk_score_emit_at = 0.0
        self.risk_score_interval_seconds = 30

        # Polígono próprio da câmera (Camera.risk_polygon); sem ele, o padrão do
        # .env. Cada câmera enxerga um trecho diferente da planta.
        risk_polygon = self._normalizar_poligono(risk_polygon) or self._parse_risk_polygon(
            app.config.get("RISK_AREA_POLYGON", Config.RISK_AREA_POLYGON)
        )
        self.risk_area_name = str(app.config.get("RISK_AREA_NAME", "Área de risco"))
        self.video_stream = VideoStream(
            source=source,
            width=width,
            height=height,
            # So tem efeito em fonte de rede (ver VideoStream._ler_do_capture).
            limiar_grab_ms=float(app.config.get("RTSP_LIMIAR_GRAB_MS", 5.0)),
        )
        # ---- modo fixture: estado de PRIMEIRA CLASSE, não degradação muda --
        # A fonte configurada da câmera, guardada separada de
        # `video_stream.source` porque o modo fixture TROCA a fonte em runtime.
        # `MonitorService._worker_config_changed` compara a fonte do worker com
        # a do banco para decidir se a câmera foi editada — se comparasse com a
        # fonte trocada, o próximo `load_cameras_from_db()` (que roda a cada
        # CRUD de câmera) veria "mudou" e reconstruiria o worker, derrubando o
        # modo fixture no meio do demo.
        self.fonte_configurada = source
        reserva = str(app.config.get("RTSP_FIXTURE_FALLBACK", "") or "")
        self.fonte_reserva = str(Path(BASE_DIR) / reserva) if reserva and not Path(reserva).is_absolute() else reserva
        self.tentativas_antes_da_reserva = max(1, int(app.config.get("RTSP_MAX_TENTATIVAS", 5)))
        self._modo_fixture = False
        self._reserva_indisponivel = False

        # ---- camada LLM: SEGUNDA OPINIÃO, nunca decisão ------------------
        # Injetado pelo MonitorService; `None` quando LLM_ENABLED=false ou não
        # há GEMINI_API_KEY — que é o default, e o demo não depende disto.
        self.servico_llm = servico_llm
        if self.servico_llm is not None:
            self.servico_llm.ao_concluir = self._consumir_analise_llm
        # A última análise, em campo PRÓPRIO. Não entra em `alerts` nem em
        # `compliance`: o LLM informa o operador, não decide por ele.
        self._segunda_opiniao: dict[str, Any] | None = None
        # Modelos compartilhados — injetados, não criados aqui (ver docstring).
        self.detector = detector
        self.person_detector = person_detector
        self.pose_estimator = pose_estimator
        self.inference_lock = inference_lock
        # Um tracker POR câmera (ver docstring de PersonTracker): os modelos
        # YOLO são compartilhados, então o estado de tracking não pode morar
        # dentro deles.
        self.person_tracker = PersonTracker()
        # Detecção intercalada (ver Config.DETECTION_EVERY_N_FRAMES). Contador
        # e último resultado são POR câmera: cada uma tem seu próprio ritmo, e
        # misturar as caixas de uma com as da outra seria pior que o lag.
        # Contador PRÓPRIO: `_frame_counter` conta frames publicados e sai no
        # status: reaproveitá-lo aqui faria a contagem andar em dobro.
        self.detect_every_n = max(1, int(app.config.get("DETECTION_EVERY_N_FRAMES", 1)))
        self._detect_counter = 0
        self._cached_analysis: FrameAnalysis | None = None
        # A histerese de alerta conta DETECÇÃO, não iteração do loop. Sem esta
        # marca, uma inferência reaproveitada por 3 frames valia 3
        # confirmações e `ALERT_CREATE_AFTER_FRAMES=3` criava alerta na
        # primeira detecção. Ver AlertStateService.process(deteccao_nova=...).
        self._analise_foi_nova = True
        # Telemetria (ver _deve_emitir_telemetria e Config.TELEMETRY_HZ).
        hz = float(app.config.get("TELEMETRY_HZ", 8.0))
        self._intervalo_telemetria = (1.0 / hz) if hz > 0 else 0.0
        self._ultima_telemetria = 0.0
        self._ultimo_diagnostico: tuple | None = None
        # Perfil por etapa do loop (ver _perf_fim). Número de frames por
        # relatório; 0 desliga e os métodos saem na primeira linha.
        self._perf_ativo = max(0, int(app.config.get("PROFILE_FRAMES", 0)))
        self._perf_t = 0.0
        self._perf_atual: dict[str, float] = {}
        self._perf_soma: dict[str, float] = {}
        self._perf_n = 0
        # Diagnostico de tela (ver `diagnostico()`). Duas janelas deslizantes,
        # porque as perguntas tem horizontes diferentes: "esta travando AGORA?"
        # olha poucos segundos, "o modelo esta vendo alguma coisa?" precisa de
        # janela longa o bastante para nao piscar entre um frame e outro.
        self._instantes_de_frame: deque[float] = deque(maxlen=200)
        self._deteccoes_recentes: deque[tuple[float, str]] = deque(maxlen=4000)
        self._resolucao_da_fonte: tuple[int, int] | None = None
        # Fonte cega (ver `brilho_do_frame` e `diagnostico`). Limiar e janela
        # vem do .env porque o que separa "imagem zerada" de "cena escura de
        # verdade" depende do local — e so a planta sabe.
        self.brilho_minimo = float(app.config.get("FONTE_BRILHO_MINIMO", 2.0))
        self.brilho_janela_s = float(app.config.get("FONTE_BRILHO_JANELA_S", 5.0))
        self._brilho: float | None = None
        self._ultima_amostra_de_brilho = 0.0
        self._escuro_desde: float | None = None
        self.rule_engine = RuleEngine(
            feature_manager=feature_manager,
            cooldown_seconds=app.config.get("ALERT_COOLDOWN_SECONDS", 0),
            risk_polygon=risk_polygon,
            supported_ppe_getter=self.detector.supported_ppe_classes,
        )
        self.alert_state_service = AlertStateService(
            AlertRepository(),
            socketio,
            create_after_frames=app.config.get("ALERT_CREATE_AFTER_FRAMES", 3),
            resolve_after_frames=app.config.get("ALERT_RESOLVE_AFTER_FRAMES", 5),
            camera_id=camera_id,
            intervalo_touch=app.config.get("ALERT_TOUCH_INTERVAL_SECONDS", 2.0),
            silencio_apos_limpar=app.config.get("ALERT_SNOOZE_AFTER_CLEAR_S", 60.0),
        )
        self.compliance_service = ComplianceService(feature_manager, self.rule_engine)
        cleanup_dirs = str(app.config.get("CLEANUP_DIRECTORIES", "runtime/snapshots,runtime/frames,runtime/tmp")).split(",")
        self.cleanup_service = StorageCleanupService(
            base_dir=Path(BASE_DIR),
            directories=cleanup_dirs,
            enabled=bool(app.config.get("CLEANUP_ON_MONITOR_START", True)),
            # Preserva snapshots que alertas do historico ainda referenciam.
            protected_files=AlertRepository().referenced_frame_filenames,
        )
        self.snapshot_service = SnapshotService(
            base_dir=Path(BASE_DIR),
            snapshot_dir=str(app.config.get("SNAPSHOT_DIR", "runtime/snapshots")),
            enabled=bool(app.config.get("SNAPSHOT_ENABLED", True)),
            jpeg_quality=int(app.config.get("SNAPSHOT_JPEG_QUALITY", 86)),
        )
        self.event_repository = EventRepository()
        self._last_cleanup: dict[str, Any] | None = None
        self.overlay_options = {
            "boxes": bool(app.config.get("OVERLAY_SHOW_BOXES", True)),
            "labels": bool(app.config.get("OVERLAY_SHOW_LABELS", True)),
            "confidence": bool(app.config.get("OVERLAY_SHOW_CONFIDENCE", True)),
            "pose": bool(app.config.get("OVERLAY_SHOW_POSE", True)),
            "risk_area": bool(app.config.get("OVERLAY_SHOW_RISK_AREA", True)),
            **{
                f"part_{parte}": parte in {p.strip() for p in str(app.config.get("OVERLAY_SHOW_PARTS", "")).split(",")}
                for parte in ("head", "face", "ear", "hands", "foot", "tool")
            },
        }
        self.annotator = FrameAnnotator(risk_polygon=risk_polygon)
        self.gate_required: list[str] | None = list(gate_required) if gate_required else None
        self._gate = GateState()

    def start(self) -> dict[str, Any]:
        with self._thread_lock:
            if self._running.is_set():
                return self.status()
            self._last_cleanup = self.cleanup_service.cleanup_startup_artifacts()
            try:
                stale_count = AlertRepository().resolve_all_active(
                    reason="monitor_start_reset",
                    camera_id=self.camera_id,
                )
                self._last_cleanup = (self._last_cleanup or {}) | {"resolved_stale_alerts": stale_count}
            except Exception as exc:  # noqa: BLE001
                logger.warning("stale_alert_cleanup_failed", extra={"error": str(exc)})
            self.alert_state_service.reset()
            self.prevention_service.reset()
            self._prevention_alerts = []
            self._emitir("active_alerts", {"camera_id": self.camera_id, "items": [], "count": 0})
            self._latest_jpeg = None
            self._latest_analysis = None
            self._last_error = None
            self._frame_counter = 0
            self._last_frame_at = None
            # Sem isto, a câmera volta exibindo as caixas da sessão anterior
            # até a primeira inferência nova concluir.
            self._detect_counter = 0
            self._cached_analysis = None
            self._last_stream_state = None
            # Idem para o diagnóstico: FPS e contagem da sessão anterior num
            # start novo fariam a tela dizer que a câmera está entregando
            # frames antes de ela ter entregado o primeiro.
            self._instantes_de_frame.clear()
            self._deteccoes_recentes.clear()
            self._resolucao_da_fonte = None
            self._brilho = None
            self._ultima_amostra_de_brilho = 0.0
            self._escuro_desde = None
            self.person_tracker.reset()
            self._running.set()
            self._task = self.socketio.start_background_task(self._loop)
            logger.info("monitor_started", extra={"camera_id": self.camera_id, "cleanup": self._last_cleanup})
            self._emit_timeline_event("monitor_started", "Monitoramento iniciado", "info", metadata={"cleanup": self._last_cleanup})
            self._emitir("monitor_status", self.status())
            return self.status()

    def stop(self) -> dict[str, Any]:
        self._running.clear()
        self.video_stream.release()
        try:
            resolved_payloads = self.alert_state_service.resolve_all(reason="monitor_stopped")
            for payload in resolved_payloads:
                self._emit_resolved_alert_event_once(payload, false_positive=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert_state_stop_resolution_failed", extra={"error": str(exc)})
        logger.info("monitor_stopped", extra={"camera_id": self.camera_id})
        self._emit_timeline_event("monitor_stopped", "Monitoramento parado", "info")
        status = self.status()
        self._emitir("monitor_status", status)
        return status

    def status(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "running": self._running.is_set(),
            "frame_counter": self._frame_counter,
            "last_error": self._last_error,
            # Estado da captura (live/reconnecting/unavailable + backoff): sem
            # isso o dashboard so via "Frame indisponivel" e nao distinguia
            # "caiu agora" de "morta ha 10 minutos".
            "video": self.video_state(),
            # FPS real do loop, resolução do frame que chegou e detecções por
            # classe nos últimos 30 s. Vai no `status()` porque é o payload que
            # o card de câmera já consulta a cada 3 s — nenhuma rota nova.
            "diagnostico": self.diagnostico(),
            "features": self.feature_manager.as_dict(),
            "model": self._safe_model_diagnostics(),
            "active_alerts": self.alert_state_service.active_alerts(),
            "compliance": (self.latest_analysis() or {}).get("compliance"),
            "cleanup": self._last_cleanup,
            "overlay": self.overlay_options,
            "settings": self.settings(),
            "risk_area": self.risk_area_state(),
            "snapshot": self.snapshot_service.info(),
            # Estado da camada de segunda opiniao. `habilitado: false` e o
            # default e nao e erro — o dashboard mostra "desligada", nao falha.
            "llm": self._estado_do_llm(),
        }

    def _estado_do_llm(self) -> dict[str, Any]:
        if self.servico_llm is None:
            return {"habilitado": False, "motivo": "LLM_ENABLED=false ou GEMINI_API_KEY ausente"}
        try:
            return dict(self.servico_llm.estatisticas()) | {"ultima": self._segunda_opiniao}
        except Exception as exc:  # noqa: BLE001  (status() nunca pode levantar: a UI cega)
            return {"habilitado": False, "motivo": redigir_segredos(str(exc))}

    def preflight(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []

        checks.append({
            "key": "backend",
            "label": "Backend",
            "status": "ok",
            "message": "API Flask respondendo",
        })
        checks.append({
            "key": "websocket",
            "label": "WebSocket",
            "status": "ok",
            "message": "SocketIO inicializado",
        })

        try:
            db.session.execute(sql_text("SELECT 1"))
            checks.append({"key": "database", "label": "Banco", "status": "ok", "message": "Conexão validada"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"key": "database", "label": "Banco", "status": "error", "message": str(exc)})

        source = self.video_stream.source
        video_message = f"Fonte configurada: {redigir_segredos(str(source))}"
        video_status = "ok"
        if isinstance(source, str) and not source.isdigit() and not source.startswith(("rtsp://", "http://", "https://")):
            video_status = "ok" if Path(source).exists() else "warning"
            fonte_visivel = redigir_segredos(str(source))
            video_message = (
                "Arquivo de vídeo encontrado"
                if Path(source).exists()
                else f"Arquivo não encontrado: {fonte_visivel}"
            )
        checks.append({"key": "video", "label": "Vídeo", "status": video_status, "message": video_message})

        model = self._safe_model_diagnostics()
        if model.get("error"):
            model_status = "error"
        elif model.get("ppe_ready"):
            model_status = "ok"
        elif model.get("warning"):
            model_status = "warning"
        else:
            model_status = "ok"
        checks.append({
            "key": "model",
            "label": "Modelo YOLO",
            "status": model_status,
            "message": model.get("warning") or "Modelo carregado",
        })

        cleanup_dirs = self.cleanup_service.directories
        checks.append({
            "key": "cleanup",
            "label": "Limpeza de testes",
            "status": "ok" if self.cleanup_service.enabled else "warning",
            "message": "Limpa ao iniciar: " + ", ".join(cleanup_dirs),
        })
        checks.append({
            "key": "snapshots",
            "label": "Evidências",
            "status": "ok" if self.snapshot_service.enabled else "warning",
            "message": f"Snapshots em {self.snapshot_service.snapshot_dir}",
        })

        summary = {
            "ok": sum(1 for item in checks if item["status"] == "ok"),
            "warning": sum(1 for item in checks if item["status"] == "warning"),
            "error": sum(1 for item in checks if item["status"] == "error"),
        }
        return {"checks": checks, "summary": summary, "can_start": summary["error"] == 0}

    def latest_jpeg(self) -> bytes | None:
        with self._thread_lock:
            return self._latest_jpeg

    def latest_jpeg_versionado(self) -> tuple[bytes | None, int]:
        """O frame e um número que muda a cada frame novo.

        O stream MJPEG usa a versão para mandar cada frame UMA vez. Antes ele
        reenviava `latest_jpeg` num relógio próprio, sem saber se havia frame
        novo: como esse relógio e o do worker não são sincronizados, o mesmo
        quadro ia repetido e outros eram pulados. No navegador isso aparece
        como engasgo — imagem parada e depois um salto — mesmo com a contagem
        de quadros parecendo alta, porque as repetições contam.
        """
        with self._thread_lock:
            return self._latest_jpeg, self._frame_counter

    def latest_analysis(self) -> dict[str, Any] | None:
        with self._thread_lock:
            return self._latest_analysis

    def _attach_snapshots_and_log_events(self, alert_state: dict[str, Any], frame) -> None:
        created_ids = {item.get("id") for item in alert_state.get("created", []) if item.get("id")}
        if created_ids:
            for runtime_state in self.alert_state_service._states.values():  # runtime state owns the live SQLAlchemy object
                alert = runtime_state.alert
                if alert is None or alert.id not in created_ids:
                    continue
                updated_alert = self.snapshot_service.attach_to_alert(alert, frame)
                payload = updated_alert.to_dict()
                runtime_state.alert = updated_alert
                self._emitir("alert_updated", payload)
                # Linha do tempo registra apenas alertas que saíram do estado ativo.
        for payload in alert_state.get("resolved", []):
            self._emit_resolved_alert_event_once(payload)

    def _emit_resolved_alert_event_once(self, payload: dict[str, Any], *, false_positive: bool = False) -> dict[str, Any] | None:
        metadata = payload.get("metadata") or {}
        subject = metadata.get("person_label") or metadata.get("person_id") or payload.get("feature")
        try:
            event = self.event_repository.create_alert_resolved_once(
                alert_payload=payload,
                message=(
                    ("Falso positivo resolvido: " if false_positive else "Alerta resolvido: ")
                    + str(payload.get("message") or "Alerta")
                ),
                severity="info",
                subject=subject,
                metadata={"false_positive": bool(false_positive)},
            )
            if event is None:
                return None
            event_payload = event.to_dict()
            self._emitir("timeline_event", event_payload)
            return event_payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("resolved_timeline_event_failed", extra={"alert_id": payload.get("id"), "error": str(exc)})
            return None

    def _emit_timeline_event(
        self,
        event_type: str,
        message: str,
        severity: str = "info",
        *,
        subject: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        try:
            event = self.event_repository.create(
                camera_id=self.camera_id,
                event_type=event_type,
                message=message,
                severity=severity,
                subject=subject,
                metadata=metadata or {},
            )
            payload = event.to_dict()
            self._emitir("timeline_event", payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline_event_failed", extra={"event_type": event_type, "error": str(exc)})
            return None

    @staticmethod
    def _parse_yolo_classes(raw: str | None) -> list[int] | None:
        if not raw:
            return None
        return [int(item.strip()) for item in str(raw).split(",") if item.strip()]

    @staticmethod
    def _parse_risk_polygon(raw: str) -> list[tuple[float, float]]:
        polygon: list[tuple[float, float]] = []
        for pair in str(raw).split(";"):
            x, y = pair.split(",")
            polygon.append((float(x), float(y)))
        return polygon

    def _conf_floors(self) -> dict[str, float]:
        """Piso de confianca POR classe de EPI (PPE_CONF_MIN_BY_CLASS).

        YOLO_CONFIDENCE e um piso unico e cego: baixo o bastante pra pegar
        oculos/luva (classes fracas) deixa entrar capacete fantasma em
        qualquer objeto amarelo. Aqui cada classe tem o seu — capacete/colete
        exigentes, oculos permissivo. Formato: "helmet:0.4,gloves:0.3".
        """
        raw = str(self.app.config.get("PPE_CONF_MIN_BY_CLASS", "") or "")
        floors: dict[str, float] = {}
        for par in raw.split(","):
            par = par.strip()
            if not par or ":" not in par:
                continue
            chave, valor = par.split(":", 1)
            try:
                floors[chave.strip()] = float(valor)
            except ValueError:
                continue
        return floors

    def _gate_ppe_to_people(self, ppe_detections: list, people: list) -> list:
        """Descarta EPI fora de qualquer pessoa e abaixo do piso da classe.

        Dois filtros baratos que atacam o falso positivo sem mexer no modelo:
        - piso de confianca por classe (ver _conf_floors);
        - a caixa do EPI precisa cair DENTRO de alguma pessoa (containment >=
          PPE_PERSON_OVERLAP_MIN). Mata capacete/luva detectados numa mochila
          no canto do quarto. So aplica quando ha pessoa detectada — sem
          referencia, preserva tudo pra nao apagar o frame inteiro.
        """
        floors = self._conf_floors()
        tem_people = bool(people)
        person_boxes = [d.box for d in people]
        person_boxes += [d.box for d in ppe_detections if d.label == "person" or d.category == "person"]
        exigir_overlap = bool(self.app.config.get("PPE_REQUIRE_PERSON_OVERLAP", True)) and bool(person_boxes)
        overlap_min = float(self.app.config.get("PPE_PERSON_OVERLAP_MIN", 0.35))

        saida = []
        for det in ppe_detections:
            if det.label == "person" or det.category == "person":
                if not tem_people:
                    saida.append(det)  # sem lista `people`, mantem como antes
                continue
            piso = floors.get(det.label)
            if piso is not None and det.confidence < piso:
                continue
            if exigir_overlap and not any(
                det.box.containment_in(pbox) >= overlap_min for pbox in person_boxes
            ):
                continue
            saida.append(det)
        return saida

    def _aplicar_rotacao(self, frame):
        """Corrige montagem física da câmera ANTES de qualquer detecção.

        Precisa ser o primeiro passo do frame — YOLO, pose (assume corpo
        vertical), overlay e o vídeo que o navegador recebe têm que ver a
        mesma imagem já corrigida. Fazer depois (ex.: só no overlay) deixaria
        o modelo analisando a imagem torta enquanto a tela mostra corrigida.
        """
        if self.rotation == 90:
            return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        if self.rotation == 180:
            return cv2.rotate(frame, cv2.ROTATE_180)
        if self.rotation == 270:
            return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return frame

    def pedir_limpeza_alertas(self) -> None:
        self._limpeza_alertas_pedida.set()

    def _limpar_alertas_ativos(self) -> None:
        resolvidos = self.alert_state_service.resolve_all(
            reason="manual_clear", silenciar_s=self.alert_state_service.silencio_apos_limpar
        )
        for payload in resolvidos:
            self._emit_resolved_alert_event_once(payload, false_positive=False)
        # Ativos que ficaram no banco sem dono (ex.: o processo anterior caiu).
        AlertRepository().resolve_all_active(reason="manual_clear", camera_id=self.camera_id)
        self._emit_timeline_event(
            "alerts_cleared", "Alertas ativos resolvidos manualmente", "info", metadata={"resolvidos": len(resolvidos)}
        )

    def _loop(self) -> None:
        jpeg_quality = int(self.app.config.get("JPEG_QUALITY", 80))

        with self.app.app_context():
            while self._running.is_set():
                if self._limpeza_alertas_pedida.is_set():
                    self._limpeza_alertas_pedida.clear()
                    try:
                        self._limpar_alertas_ativos()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("limpeza_de_alertas_falhou", extra={"error": str(exc)})
                start_time = time.perf_counter()
                self._perf_inicio()
                try:
                    ok, frame = self.video_stream.read()
                    self._perf_marca("captura")
                    if not ok or frame is None:
                        self._handle_capture_failure()
                        continue
                    observation_time = time.monotonic()
                    if self.rotation:
                        frame = self._aplicar_rotacao(frame)

                    analysis = self._analyze_frame(frame)
                    self._registrar_diagnostico(frame, analysis, self._analise_foi_nova)
                    self._perf_marca("analise")
                    # UMA passada de regras por frame. O resultado (alertas +
                    # estado por pessoa) alimenta tanto o AlertStateService
                    # quanto o ComplianceService — que antes refazia todo o
                    # trabalho por conta própria.
                    evaluation = self.rule_engine.analyze(
                        analysis.detections,
                        analysis.pose,
                        frame.shape,
                        poses=analysis.poses,
                    )
                    self._perf_marca("regras")
                    prevention, prevention_alerts = self.prevention_service.process(
                        evaluation.people, evaluation.alerts, frame, observation_time, fresh=self._analise_foi_nova,
                        risk_enabled=self.feature_manager.is_enabled("risk_area"),
                        posture_enabled=self.feature_manager.is_enabled("pose") and self.feature_manager.is_enabled("posture"),
                    )
                    if self._analise_foi_nova:
                        self._prevention_alerts = prevention_alerts
                    evaluation = RuleEvaluation(evaluation.alerts + self._prevention_alerts, evaluation.people, evaluation.poses)
                    # `deteccao_nova` impede que uma inferência reaproveitada
                    # conte como confirmação nova na histerese.
                    alert_state = self.alert_state_service.process(
                        evaluation.alerts,
                        deteccao_nova=self._analise_foi_nova,
                        features_desligadas=self._features_desligadas(),
                    )
                    self._perf_marca("alertas")
                    model_diagnostics = self._safe_model_diagnostics()
                    compliance_state = self.compliance_service.build_state(
                        detections=analysis.detections,
                        pose=analysis.pose,
                        frame_shape=frame.shape,
                        model_diagnostics=model_diagnostics,
                        active_alerts=alert_state["active"],
                        evaluation=evaluation,
                    )

                    if self.gate_required:
                        self._gate.atualizar(avaliar_quadro(compliance_state, self.gate_required))

                    compliance_state["prevention"] = prevention

                    enabled_map = {item.key: item.enabled for item in self.feature_manager.list()}
                    annotated = self.annotator.annotate(
                        frame,
                        analysis.detections,
                        analysis.poses or ([analysis.pose] if analysis.pose else []),
                        enabled_map,
                        compliance_state,
                        self.overlay_options,
                    )
                    self._perf_marca("anotacao")
                    if self.overlay_options.get("risk_area", True):
                        draw_prevention(annotated, prevention)
                    self._attach_snapshots_and_log_events(alert_state, annotated)
                    alert_state["active"] = self.alert_state_service.active_alerts()
                    encode_ok, buffer = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
                    self._perf_marca("encode")
                    jpeg: bytes | None = None
                    if encode_ok:
                        jpeg = buffer.tobytes()
                        with self._thread_lock:
                            self._latest_jpeg = jpeg
                            self._latest_analysis = analysis.to_dict() | {
                                "alerts": alert_state["active"],
                                "alert_changes": alert_state["changed"],
                                "alert_resolved": alert_state["resolved"],
                                "compliance": compliance_state,
                                "model": model_diagnostics,
                                "prevention": prevention,
                                # Campo PRÓPRIO, ao lado dos alertas e nunca
                                # dentro deles: é opinião, não veredito.
                                "segunda_opiniao": self._segunda_opiniao,
                            }
                            self._frame_counter += 1
                            self._last_frame_at = time.monotonic()

                    # Fora da thread do loop: `submeter()` só marca o slot e
                    # entrega ao executor (11 microssegundos, docs/SPRINT3.md).
                    # Reaproveita o JPEG acima — não há segundo encode.
                    self._submeter_ao_llm(alert_state, jpeg)

                    # camera_id em TODO payload: o dashboard usa isso pra
                    # descartar o que não é da câmera em foco. Sem o carimbo,
                    # duas câmeras rodando sobrescreviam o estado uma da outra
                    # a cada frame.
                    if self._last_stream_state not in (None, "live"):
                        self._last_stream_state = "live"
                        self._emit_timeline_event(
                            "camera_reconnected",
                            "Sinal restabelecido",
                            "info",
                            metadata=self.video_stream.status().to_dict(),
                        )
                        self._emitir("monitor_status", self.status())

                    # Telemetria com taxa própria, separada da do vídeo. O
                    # `analysis` sozinho pesa ~26 KB (detecções + landmarks de
                    # pose por pessoa); mandá-lo a 24 FPS por duas câmeras
                    # empurrava ~1,2 MB/s pro navegador, e cada evento dispara
                    # re-render. As caixas que aparecem no vídeo NÃO dependem
                    # disto — vêm desenhadas no MJPEG.
                    #
                    # Mudança de alerta fura o intervalo: quando alguém tira o
                    # capacete, o painel tem que reagir na hora, não no
                    # próximo tique.
                    mudou_alerta = bool(alert_state["changed"] or alert_state["resolved"])
                    if mudou_alerta or self._deve_emitir_telemetria():
                        payload = self.latest_analysis() or {}
                        self._emitir("analysis", payload | {"camera_id": self.camera_id})
                        self._emitir("compliance_state", compliance_state | {"camera_id": self.camera_id})
                        self._emitir_diagnostico_se_mudou(model_diagnostics)
                    self._maybe_emit_risk_score()
                    self._perf_marca("emissao")
                    self._perf_fim()
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._last_error = redigir_segredos(str(exc))
                    logger.exception("monitor_loop_error", extra={"camera_id": self.camera_id, "error": str(exc)})
                    time.sleep(1.0)

                elapsed = time.perf_counter() - start_time
                time.sleep(max(0.0, 1.0 / self.target_fps - elapsed))

    # ------------------------------------------------------- perfil do loop -
    # Diagnóstico de "por que o vídeo está travado". FPS médio engana: quadros
    # repetidos inflam a contagem e uma etapa cara esconde-se na média. Ligado
    # por PROFILE_FRAMES no .env (0 = desligado, sem custo).
    def _emitir(self, evento: str, payload) -> None:
        """Emite respeitando o escopo de camera do Operador.

        Ver `app/utils/salas.py`. Antes disto todo emit era broadcast e o
        operador de um setor recebia o feed de todos os outros.
        """
        emitir_para_camera(self.socketio, evento, payload, self.camera_id)

    def _perf_inicio(self) -> None:
        if not self._perf_ativo:
            return
        self._perf_t = time.perf_counter()
        self._perf_atual = {}

    def _perf_marca(self, etapa: str) -> None:
        if not self._perf_ativo:
            return
        agora = time.perf_counter()
        self._perf_atual[etapa] = (agora - self._perf_t) * 1000.0
        self._perf_t = agora

    def _perf_fim(self) -> None:
        if not self._perf_ativo:
            return
        for etapa, ms in self._perf_atual.items():
            self._perf_soma[etapa] = self._perf_soma.get(etapa, 0.0) + ms
        self._perf_n += 1
        if self._perf_n < self._perf_ativo:
            return
        media = {k: round(v / self._perf_n, 1) for k, v in sorted(self._perf_soma.items(), key=lambda kv: -kv[1])}
        total = round(sum(media.values()), 1)
        logger.warning(
            "perfil_do_frame",
            extra={
                "camera_id": self.camera_id,
                "frames": self._perf_n,
                "ms_por_etapa": media,
                "ms_total": total,
                "fps_possivel": round(1000.0 / total, 1) if total else None,
            },
        )
        self._perf_soma, self._perf_n = {}, 0

    def _deve_emitir_telemetria(self) -> bool:
        """True no máximo TELEMETRY_HZ vezes por segundo."""
        if self._intervalo_telemetria <= 0:
            return True
        agora = time.perf_counter()
        if agora - self._ultima_telemetria < self._intervalo_telemetria:
            return False
        self._ultima_telemetria = agora
        return True

    def _emitir_diagnostico_se_mudou(self, diagnostics: dict[str, Any]) -> None:
        """Só manda o diagnóstico do modelo quando ele muda de verdade.

        São ~1,3 KB com a lista das 14 classes — conteúdo que só muda se
        alguém trocar de modelo ou o carregamento falhar. Reenviar a cada
        frame era puro desperdício de banda e de re-render.
        """
        assinatura = (
            diagnostics.get("model_path"),
            diagnostics.get("ppe_ready"),
            diagnostics.get("error"),
            diagnostics.get("warning"),
            diagnostics.get("raw_class_count"),
        )
        if assinatura == self._ultimo_diagnostico:
            return
        self._ultimo_diagnostico = assinatura
        self._emitir("model_diagnostics", diagnostics | {"camera_id": self.camera_id})

    def _submeter_ao_llm(self, alert_state: dict[str, Any], jpeg: bytes | None) -> None:
        """Dispara a análise multimodal quando um alerta é CRIADO.

        Três decisões, cada uma com um número atrás:

        - **Só em alerta criado.** Um alerta ativo persiste por segundos; a
          fixture de 7 s gera 26 alertas mesmo depois do fix da Fase 1
          (docs/BENCH.md). Disparar por frame estouraria a cota do free tier.
          O debounce do serviço é a segunda linha de defesa, não a primeira.
        - **Reaproveita o JPEG que já foi codificado.** Há exatamente um
          `cv2.imencode` por frame (2,5 ms, docs/BENCH.md) e o resultado serve
          todos os consumidores. Codificar de novo aqui somaria 2,5 ms ao
          caminho do frame justamente nos frames com alerta.
        - **Nunca levanta.** Bug no serviço ou provedor fora do ar não pode
          parar a câmera. `submeter()` já é não-bloqueante — medido em 11
          microssegundos (docs/SPRINT3.md) — mas o `try` cobre o resto.
        """
        if self.servico_llm is None or not jpeg or not alert_state.get("created"):
            return
        try:
            self.servico_llm.submeter(camera_id=self.camera_id, imagem_jpeg=jpeg)
        except Exception as exc:  # noqa: BLE001  (camada opcional nao derruba a camera)
            logger.warning("llm_submissao_falhou", extra={"camera_id": self.camera_id, "error": str(exc)})

    def _consumir_analise_llm(self, analise) -> None:
        """Recebe a análise pronta, no executor do serviço — não no loop.

        Guarda em campo separado e emite. **Não** cria, resolve ou suprime
        alerta, e nem toca no estado de conformidade: se um modelo de linguagem
        pudesse apagar um alerta de EPI, o sistema passaria a esconder violação
        de segurança do trabalho com base em texto gerado. Ele informa o
        operador; quem decide é o operador.
        """
        payload = analise.model_dump() if hasattr(analise, "model_dump") else dict(analise)
        with self._thread_lock:
            self._segunda_opiniao = payload
            if self._latest_analysis is not None:
                self._latest_analysis = self._latest_analysis | {"segunda_opiniao": payload}
        self._emitir("llm_segunda_opiniao", payload | {"camera_id": self.camera_id})

    def diagnostico(self) -> dict[str, Any]:
        """As três perguntas de campo, respondidas sem abrir terminal.

        Existe porque o FPS que o dashboard mostrava **não era o do pipeline**:
        `dashboardStore.ts` derivava-o do intervalo entre eventos `analysis`, e
        esses eventos são emitidos no máximo `TELEMETRY_HZ` vezes por segundo
        (8, por padrão). O número tinha teto em 8 e não distinguia "o pipeline
        caiu para 6 fps" de "o pipeline está a 19 e a telemetria está limitada".
        Aqui o FPS é contado no próprio loop de captura, por câmera.

        `resolucao` é a do frame que **chegou**, não a pedida no cadastro: para
        fonte RTSP o `CAP_PROP_FRAME_WIDTH` é um pedido que o backend FFMPEG
        ignora, então o que está no banco pode não ser o que a câmera entrega.
        É o campo que separa "o substream é pequeno demais" de "o modelo não
        está achando nada".

        `deteccoes_30s` conta por classe, e só quando houve **inferência
        nova**: com `DETECTION_EVERY_N_FRAMES=3` os frames intermediários
        reaproveitam as caixas anteriores, e contá-los infla a contagem em 3x
        sem o modelo ter rodado. Zero aqui com `resolucao` boa e FPS saudável
        aponta para o modelo; zero com FPS no chão aponta para a fonte.
        """
        agora = time.monotonic()
        instantes = [t for t in self._instantes_de_frame if agora - t <= JANELA_FPS_S]
        frame_age = None if self._last_frame_at is None else max(0.0, agora - self._last_frame_at)
        fps = 0.0
        if len(instantes) >= 2:
            decorrido = instantes[-1] - instantes[0]
            # n-1 intervalos entre n amostras. Usar n dividido pela janela
            # inteira contaria um intervalo a mais e inflaria o FPS quando a
            # janela está parcialmente preenchida (logo após o start).
            fps = (len(instantes) - 1) / decorrido if decorrido > 0 else 0.0

        por_classe: dict[str, int] = {}
        for instante, rotulo in self._deteccoes_recentes:
            if agora - instante <= JANELA_DETECCOES_S:
                por_classe[rotulo] = por_classe.get(rotulo, 0) + 1

        largura, altura = self._resolucao_da_fonte or (0, 0)
        # `fonte_sem_imagem` e DIAGNOSTICO, nao veredito: nao para o worker e
        # nao cria alerta. Cena legitimamente escura nao e falha, e derrubar a
        # captura por causa dela seria pior que o silencio que isto conserta.
        sem_imagem = (
            self._escuro_desde is not None
            and (agora - self._escuro_desde) >= self.brilho_janela_s
        )
        return {
            "fps": round(fps, 1),
            "idade_frame_s": None if frame_age is None else round(frame_age, 2),
            "imagem_desatualizada": frame_age is None or frame_age > max(2.0, 3.0 / self.target_fps),
            "brilho": None if self._brilho is None else round(self._brilho, 2),
            "fonte_sem_imagem": bool(sem_imagem),
            "brilho_minimo": self.brilho_minimo,
            "resolucao": f"{largura}x{altura}" if largura else None,
            "largura": largura,
            "altura": altura,
            "deteccoes_30s": dict(sorted(por_classe.items(), key=lambda kv: (-kv[1], kv[0]))),
            "janela_deteccoes_s": JANELA_DETECCOES_S,
        }

    def _registrar_diagnostico(self, frame, analise: FrameAnalysis, foi_nova: bool) -> None:
        """Alimenta as janelas de `diagnostico()`. Chamado uma vez por frame."""
        agora = time.monotonic()
        self._instantes_de_frame.append(agora)
        altura, largura = frame.shape[:2]
        self._resolucao_da_fonte = (largura, altura)
        self._amostrar_brilho(frame, agora)
        if not foi_nova:
            return
        for deteccao in analise.detections:
            self._deteccoes_recentes.append((agora, deteccao.label))

    def _amostrar_brilho(self, frame, agora: float) -> None:
        """Mede brilho no maximo uma vez por `INTERVALO_AMOSTRA_BRILHO_S`.

        Guarda desde QUANDO o brilho esta abaixo do limiar, em vez de contar
        frames escuros: contagem de frames dependeria do FPS, e a mesma
        escuridao acusaria em tempos diferentes numa camera de 25 fps e numa
        de 6. O que o operador percebe e tempo.
        """
        if agora - self._ultima_amostra_de_brilho < INTERVALO_AMOSTRA_BRILHO_S:
            return
        self._ultima_amostra_de_brilho = agora
        self._brilho = brilho_do_frame(frame)
        if self._brilho < self.brilho_minimo:
            # Primeira amostra escura marca o inicio; as seguintes nao mexem,
            # senao a janela nunca fecharia.
            if self._escuro_desde is None:
                self._escuro_desde = agora
        else:
            self._escuro_desde = None

    def video_state(self) -> dict[str, Any]:
        """Estado da captura + em QUAL fonte a câmera está.

        `state` (idle/live/reconnecting/unavailable) responde "está entregando
        frame?". `modo` responde "de qual fonte?", que é a pergunta que o
        dashboard precisa para mostrar "modo fixture — fonte de demonstração"
        em vez de "reconectando". São ortogonais de propósito: uma câmera em
        modo fixture está `live` e em `fixture` ao mesmo tempo.

        Com a câmera parada (`idle`) o modo é `ao_vivo` porque a fonte que vale
        é a configurada — o `running: false` do `status()` é o que diz à UI para
        não desenhar badge de conexão nenhuma.
        """
        estado = self.video_stream.status().to_dict()
        if self._modo_fixture:
            modo = "fixture"
        elif estado["state"] in ("reconnecting", "unavailable"):
            modo = "reconectando"
        else:
            modo = "ao_vivo"
        return estado | {
            "modo": modo,
            # A fonte em uso, redigida: e o que distingue "fixture" de "ao
            # vivo" na tela sem obrigar o operador a abrir o banco.
            "fonte": redigir_segredos(str(self.video_stream.source)),
        }

    def _assumir_fonte_reserva(self) -> None:
        """Troca para a fonte de demonstração, em loop, e anuncia.

        Isto é o seguro do demo: sem rota para a rede da planta, uma câmera
        RTSP fica indisponível para sempre e não há imagem nenhuma na tela. Com
        a troca, o demo continua — e diz na tela que continua com fonte de
        demonstração, que é a leitura honesta.
        """
        if not Path(self.fonte_reserva).exists():
            # Fixture não é versionada (ver docs/FIXTURES.md). Sem ela, mentir
            # "modo fixture" seria pior que ficar em reconectando: a tela diria
            # que há imagem de demonstração e não haveria.
            self._reserva_indisponivel = True
            logger.error(
                "fixture_de_reserva_ausente",
                extra={
                    "camera_id": self.camera_id,
                    "caminho": self.fonte_reserva,
                    "hint": "rode: python scripts/fetch_fixtures.py",
                },
            )
            return

        self.video_stream.release()
        self.video_stream = VideoStream(
            source=self.fonte_reserva,
            width=self.video_stream.width,
            height=self.video_stream.height,
            em_loop=True,
        )
        self._modo_fixture = True
        self._last_stream_state = None  # força reemitir o estado novo
        logger.warning(
            "modo_fixture_assumido",
            extra={
                "camera_id": self.camera_id,
                "fonte_configurada": redigir_segredos(str(self.fonte_configurada)),
                "tentativas": self.tentativas_antes_da_reserva,
            },
        )
        self._emit_timeline_event(
            "camera_modo_fixture",
            f"Modo fixture — fonte de demonstração após {self.tentativas_antes_da_reserva} tentativas",
            "warning",
            metadata=self.video_state(),
        )
        self._emitir("monitor_status", self.status())

    def _handle_capture_failure(self) -> None:
        """Frame nao veio. Anota o estado, avisa a UI quando ele MUDA e dorme
        o tempo certo — nem loop apertado, nem parado alem do backoff."""
        estado = self.video_stream.status()
        self._last_error = estado.last_error or "Frame indisponível"

        # Teto de tentativas na fonte configurada: assume a fixture e segue.
        # Uma vez em modo fixture nao volta sozinho — voltar exigiria sondar a
        # fonte morta em paralelo, e trocar a imagem no meio de uma
        # apresentacao e pior que ficar na fonte que funciona.
        if (
            not self._modo_fixture
            and not self._reserva_indisponivel
            and self.fonte_reserva
            and estado.reconnect_attempts >= self.tentativas_antes_da_reserva
        ):
            self._assumir_fonte_reserva()
            return

        if estado.state != self._last_stream_state:
            self._last_stream_state = estado.state
            if estado.state in ("reconnecting", "unavailable"):
                self._emit_timeline_event(
                    "camera_disconnected",
                    f"Sinal perdido — tentando reconectar (tentativa {estado.reconnect_attempts + 1})",
                    "warning",
                    metadata=estado.to_dict(),
                )
            self._emitir("monitor_status", self.status())

        # Dentro da janela de backoff nao adianta girar a 12 FPS; fora dela,
        # 0.2s mantem a resposta rapida quando a fonte volta.
        time.sleep(min(max(estado.seconds_until_retry, 0.2), 1.0))

    def _analyze_frame(self, frame) -> FrameAnalysis:
        # Detecção intercalada: com detect_every_n > 1, os frames do meio
        # reaproveitam as caixas da última inferência em vez de rodar o modelo.
        # O vídeo continua saindo na taxa de captura (fluido) enquanto a
        # detecção anda no ritmo que a máquina aguenta.
        #
        # O que isso custa: as caixas ficam até (N-1) frames defasadas em
        # relação ao vídeo. Como EPI não aparece e some entre frames, a
        # conformidade não muda; o que "atrasa" é a caixa acompanhar alguém em
        # movimento. Por isso o padrão é 1 — quem liga escolhe essa troca.
        self._detect_counter += 1
        if self.detect_every_n > 1 and self._cached_analysis is not None:
            if self._detect_counter % self.detect_every_n != 0:
                self._analise_foi_nova = False
                return self._cached_analysis
        self._analise_foi_nova = True

        detections = []
        pose = None
        poses: list[PoseResult] = []
        # inference_lock: modelos são compartilhados entre workers (Passo 4)
        # — serializa quem usa a GPU por vez. Numa GPU só isso não perde
        # paralelismo real (ela já processa um kernel de cada vez), só evita
        # dois threads chamando forward() no mesmo objeto simultaneamente.
        self._perf_marca("espera_do_lock_pre")
        with self.inference_lock:
            self._perf_marca("espera_do_lock")
            if self._needs_yolo_detection():
                ppe_detections = self.detector.detect(frame)
                self._perf_marca("yolo")
                if self.app.config.get("MULTI_PERSON_DETECTION", True):
                    # Só as pessoas passam pelo tracker — EPIs são associados
                    # geometricamente a elas (PersonComplianceMatcher), não
                    # rastreados por conta própria.
                    raw_people = self.person_detector.detect(frame)
                else:
                    raw_people = [d for d in ppe_detections if d.label == "person" or d.category == "person"]
                people = self.person_tracker.update(raw_people)
                ppe_detections = [d for d in ppe_detections if d.label != "person" and d.category != "person"]
                ppe_detections = self._gate_ppe_to_people(ppe_detections, people)
                detections = ppe_detections + people
            if self.feature_manager.is_enabled("pose"):
                poses = self._estimate_poses(frame, detections)
                self._perf_marca("pose")
                # `pose` continua sendo a primeira (ou a global) pra nao quebrar
                # quem ja lia esse campo — frontend inclusive.
                pose = poses[0] if poses else None
        analise = FrameAnalysis(detections=detections, pose=pose, risk_events=[], poses=poses)
        self._cached_analysis = analise
        return analise

    def _estimate_poses(self, frame, detections) -> list[PoseResult]:
        """Uma pose por pessoa quando ha caixa de pessoa; senao, a global.

        A pose global cobre o caso em que o modelo nao detectou ninguem mas o
        MediaPipe ainda encontra um corpo — comportamento de sempre, mantido
        como rede de seguranca.
        """
        if not bool(self.app.config.get("POSE_PER_PERSON", True)):
            global_pose = self.pose_estimator.estimate(frame)
            return [global_pose] if global_pose.found else []

        pessoas = [item for item in detections if item.label == "person" or item.category == "person"]
        pessoas.sort(key=lambda item: item.track_id if item.track_id is not None else 0)
        alvos = [
            (
                f"person_{item.track_id}" if item.track_id is not None else f"person_{indice}",
                item.track_id,
                item.box,
            )
            for indice, item in enumerate(pessoas, start=1)
        ]
        if alvos:
            poses = self.pose_estimator.estimate_for_people(
                frame,
                alvos,
                max_people=int(self.app.config.get("POSE_MAX_PEOPLE", 4)),
            )
            if poses:
                return poses

        global_pose = self.pose_estimator.estimate(frame)
        return [global_pose] if global_pose.found else []

    def _features_desligadas(self) -> set[str]:
        """Features cujos alertas devem acabar: as desligadas, mais as que
        dependem de uma chave-mestra desligada (ppe -> cada EPI; pose -> queda e postura)."""
        desligadas = {item.key for item in self.feature_manager.list() if not item.enabled}
        if "ppe" in desligadas:
            desligadas |= set(PPE_KEYS)
        if "pose" in desligadas:
            desligadas |= {"falls", "posture"}
        if not self.prevention_service.config["enabled"]:
            desligadas.add("prevention")
        return desligadas

    def _needs_yolo_detection(self) -> bool:
        return bool(
            self.app.config.get("MULTI_PERSON_DETECTION", True)
            or self.feature_manager.is_enabled("ppe")
            or self.feature_manager.is_enabled("risk_area")
            or self.prevention_service.config["enabled"]
        )

    def _safe_model_diagnostics(self) -> dict[str, Any]:
        if not self._needs_yolo_detection():
            return {
                "model_path": self.detector.model_path,
                "ppe_ready": False,
                "supported_ppe": {"helmet": False, "vest": False, "gloves": False},
                "person_supported": False,
                "warning": "YOLO desativado pelas features atuais",
                "error": None,
            }
        diagnostics = dict(self.detector.diagnostics())
        multi_person = bool(self.app.config.get("MULTI_PERSON_DETECTION", True))
        person_supported = bool(diagnostics.get("person_supported"))
        warning_parts = [diagnostics.get("warning")] if diagnostics.get("warning") else []
        if multi_person:
            person_diagnostics = self.person_detector.diagnostics()
            person_supported = bool(person_diagnostics.get("person_supported"))
            diagnostics["person_model_path"] = self.person_detector.model_path
            if not person_supported:
                warning_parts.append(
                    f"Modelo de pessoa ({self.person_detector.model_path}) não possui classe 'person'."
                    if not person_diagnostics.get("error")
                    else f"Modelo de pessoa indisponível: {person_diagnostics.get('error')}"
                )
        diagnostics["person_supported"] = person_supported
        diagnostics["ppe_ready"] = bool(diagnostics.get("ppe_ready")) and (person_supported if multi_person else True)
        diagnostics["warning"] = " ".join(warning_parts) or None
        diagnostics["ppe_feature_enabled"] = self.feature_manager.is_enabled("ppe")
        diagnostics["multi_person_detection"] = multi_person
        return diagnostics

    def _maybe_emit_risk_score(self) -> None:
        # ponytail: throttle por relógio, não por contagem de frame — TARGET_FPS
        # é configurável em runtime, contar frames faria o intervalo derrapar.
        now = time.time()
        if now - self._last_risk_score_emit_at < self.risk_score_interval_seconds:
            return
        self._last_risk_score_emit_at = now
        try:
            self._emitir("risk_score", compute_risk_score(camera_id=self.camera_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("risk_score_emit_failed", extra={"error": str(exc)})

    def settings(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "video_source": redigir_segredos(str(self.video_stream.source)),
            "target_fps": self.target_fps,
            "jpeg_quality": int(self.app.config.get("JPEG_QUALITY", 80)),
            "yolo_confidence": float(self.detector.confidence),
            "yolo_max_detections": int(self.detector.max_detections),
            "multi_person_detection": bool(self.app.config.get("MULTI_PERSON_DETECTION", True)),
            "alert_create_after_frames": int(self.alert_state_service.create_after_frames),
            "alert_resolve_after_frames": int(self.alert_state_service.resolve_after_frames),
            "cleanup_on_monitor_start": bool(self.cleanup_service.enabled),
            "snapshot_enabled": bool(self.snapshot_service.enabled),
            "snapshot_jpeg_quality": int(self.snapshot_service.jpeg_quality),
            "risk_area_name": self.risk_area_name,
        }

    def update_settings(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed_ints = {
            "target_fps": ("TARGET_FPS", 1, 60),
            "jpeg_quality": ("JPEG_QUALITY", 40, 100),
            "yolo_max_detections": ("YOLO_MAX_DETECTIONS", 1, 300),
            "alert_create_after_frames": ("ALERT_CREATE_AFTER_FRAMES", 1, 60),
            "alert_resolve_after_frames": ("ALERT_RESOLVE_AFTER_FRAMES", 1, 120),
            "snapshot_jpeg_quality": ("SNAPSHOT_JPEG_QUALITY", 40, 100),
        }
        for key, (config_key, minimum, maximum) in allowed_ints.items():
            if key not in updates:
                continue
            value = max(minimum, min(maximum, int(updates[key])))
            if key == "target_fps":
                self.target_fps = value
                continue
            self.app.config[config_key] = value
            if key == "yolo_max_detections":
                self.detector.max_detections = value
            elif key == "alert_create_after_frames":
                self.alert_state_service.create_after_frames = value
            elif key == "alert_resolve_after_frames":
                self.alert_state_service.resolve_after_frames = value
            elif key == "snapshot_jpeg_quality":
                self.snapshot_service.jpeg_quality = value

        if "yolo_confidence" in updates:
            value = max(0.05, min(0.95, float(updates["yolo_confidence"])))
            self.app.config["YOLO_CONFIDENCE"] = value
            self.detector.confidence = value
        if "multi_person_detection" in updates:
            self.app.config["MULTI_PERSON_DETECTION"] = bool(updates["multi_person_detection"])
        if "cleanup_on_monitor_start" in updates:
            self.cleanup_service.enabled = bool(updates["cleanup_on_monitor_start"])
        if "snapshot_enabled" in updates:
            self.snapshot_service.enabled = bool(updates["snapshot_enabled"])
        if "risk_area_name" in updates and str(updates["risk_area_name"]).strip():
            self.risk_area_name = str(updates["risk_area_name"]).strip()[:80]
        self._emit_timeline_event(
            "settings_updated",
            "Configurações runtime atualizadas",
            "info",
            metadata={"updates": list(updates.keys())},
        )
        self._emitir("settings_updated", self.settings())
        return self.settings()

    def risk_area_state(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "name": self.risk_area_name,
            "polygon": [{"x": round(float(x), 4), "y": round(float(y), 4)} for x, y in self.rule_engine.risk_polygon],
            "enabled": self.feature_manager.is_enabled("risk_area"),
        }

    @staticmethod
    def _normalizar_poligono(raw) -> list[tuple[float, float]] | None:
        """Aceita lista de {x,y} ou [x,y]; devolve pontos 0..1, ou None se vazio.

        Levanta ValueError se vier algo preenchido mas inválido — melhor
        recusar do que salvar uma zona quebrada em silêncio.
        """
        if raw is None or raw == []:
            return None
        if not isinstance(raw, list) or len(raw) < 3:
            raise ValueError("polygon deve conter pelo menos 3 pontos")
        polygon: list[tuple[float, float]] = []
        for point in raw:
            if isinstance(point, dict):
                x, y = point.get("x"), point.get("y")
            elif isinstance(point, (list, tuple)) and len(point) >= 2:
                x, y = point[0], point[1]
            else:
                raise ValueError("cada ponto deve conter x e y")
            try:
                xf, yf = float(x), float(y)
            except (TypeError, ValueError) as exc:
                raise ValueError("cada ponto deve conter x e y numéricos") from exc
            polygon.append((max(0.0, min(1.0, xf)), max(0.0, min(1.0, yf))))
        return polygon

    def update_risk_area(self, payload: dict[str, Any]) -> dict[str, Any]:
        polygon = self._normalizar_poligono(payload.get("polygon"))
        if polygon is None:
            raise ValueError("polygon deve conter pelo menos 3 pontos")
        self.rule_engine.risk_polygon = polygon
        self.annotator.risk_polygon = polygon
        if str(payload.get("name", "")).strip():
            self.risk_area_name = str(payload.get("name")).strip()[:80]
        state = self.risk_area_state()
        self._emit_timeline_event("risk_area_updated", "Área de risco atualizada", "info", metadata=state)
        self._emitir("risk_area_updated", state)
        return state

    def gate_state(self) -> dict[str, Any]:
        base = {"camera_id": self.camera_id, "enabled": bool(self.gate_required), "required": self.gate_required or []}
        if not self.gate_required:
            return base | {"verdict": "off", "missing": [], "people": 0}
        return base | self._gate.atual

    def set_gate(self, required: list[str] | None) -> None:
        self.gate_required = list(required) if required else None
        self._gate.reset()

    def get_overlay(self) -> dict[str, Any]:
        return {"camera_id": self.camera_id, **self.overlay_options}

    def update_overlay(self, updates: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "boxes", "labels", "confidence", "pose", "risk_area",
            *(f"part_{p}" for p in ("head", "face", "ear", "hands", "foot", "tool")),
        )
        for key in keys:
            if key in updates:
                self.overlay_options[key] = bool(updates[key])
        self._emitir("overlay_updated", self.get_overlay())
        return self.get_overlay()
