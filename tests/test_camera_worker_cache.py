"""Caracterização da detecção intercalada do `CameraWorker`.

`camera_worker.py` está a 34% de cobertura e o loop de frame não é exercitado
por nenhum teste. Este arquivo documenta o comportamento ATUAL de
`_analyze_frame` — não o desejado — porque é ele que explica um bug medido de
ciclo de vida de alerta.

Com `DETECTION_EVERY_N_FRAMES=3`, dois de cada três frames **reaproveitam o
mesmo objeto `FrameAnalysis`** da última inferência. Consequência que motivou
estes testes: `ALERT_CREATE_AFTER_FRAMES=3` conta frames do LOOP, então uma
única inferência real é vista três vezes e satisfaz sozinha a histerese que
está documentada como "cria após 3 frames ruins". Medido na fixture de 7 s:
tracks vistos em UMA única detecção criaram 5 alertas cada.

Nada aqui abre câmera nem carrega peso: os detectores são injetados no
construtor, e os dublês contam chamadas.
"""

from __future__ import annotations

import threading

import numpy as np
import pytest
from app import create_app
from app.config import TestConfig
from app.services.camera_worker import CameraWorker
from app.services.feature_manager import FeatureManager
from app.vision.schemas import BoundingBox, Detection, PoseResult


class DetectorDuble:
    """Conta quantas vezes a inferencia foi realmente pedida."""

    def __init__(self, deteccoes: list[Detection] | None = None) -> None:
        self.chamadas = 0
        self.confidence = 0.35
        self.max_detections = 100
        self._deteccoes = deteccoes or []

    def detect(self, frame):  # noqa: ARG002
        self.chamadas += 1
        return list(self._deteccoes)

    def diagnostics(self):
        return {"model_path": "duble.pt", "classes": {}, "error": None}

    def supported_ppe_classes(self):
        """O `RuleEngine` recebe isto como getter (camera_worker.py:133)."""
        return {"helmet", "vest", "gloves", "glasses", "mask", "safety_shoe"}


class PoseDuble:
    def __init__(self) -> None:
        self.chamadas_globais = 0
        self.chamadas_por_pessoa = 0

    def estimate(self, frame):  # noqa: ARG002
        self.chamadas_globais += 1
        return PoseResult(landmarks=[])

    def estimate_for_people(self, frame, people, *, max_people=4):  # noqa: ARG002
        self.chamadas_por_pessoa += 1
        return []


def pessoa(track_id: int = 1) -> Detection:
    return Detection(
        label="person",
        confidence=0.9,
        box=BoundingBox(x1=10, y1=10, x2=110, y2=310),
        category="person",
        track_id=track_id,
    )


@pytest.fixture()
def worker():
    class Cfg(TestConfig):
        DETECTION_EVERY_N_FRAMES = 3
        MULTI_PERSON_DETECTION = False
        POSE_PER_PERSON = True

    app = create_app(Cfg)
    with app.app_context():
        trabalhador = CameraWorker(
            app,
            socketio=None,
            feature_manager=FeatureManager.from_config(app.config),
            camera_id=1,
            source="fonte-inexistente-nunca-aberta",
            fps=12,
            detector=DetectorDuble([pessoa()]),
            person_detector=DetectorDuble(),
            pose_estimator=PoseDuble(),
            inference_lock=threading.Lock(),
        )
        yield trabalhador


FRAME = np.zeros((540, 960, 3), dtype=np.uint8)


def test_infere_em_um_de_cada_tres_frames(worker):
    """Seis frames de loop produzem TRES inferencias, nao seis."""
    for _ in range(6):
        worker._analyze_frame(FRAME)

    assert worker.detector.chamadas == 3, (
        f"esperava 3 inferencias em 6 frames com detect_every_n=3, houve {worker.detector.chamadas}"
    )


def test_frames_intermediarios_devolvem_o_mesmo_objeto_de_analise(worker):
    """Nao e uma copia equivalente: e a mesma instancia, reaproveitada.

    E esta identidade que torna a histerese enganosa — quem conta frames do
    loop conta o mesmo resultado tres vezes.
    """
    primeiro = worker._analyze_frame(FRAME)
    segundo = worker._analyze_frame(FRAME)
    terceiro = worker._analyze_frame(FRAME)

    assert segundo is primeiro, "frame 2 deveria reaproveitar a analise do frame 1"
    assert terceiro is not primeiro, "frame 3 e frame de deteccao: deveria ser analise nova"
    assert worker.detector.chamadas == 2


def test_uma_deteccao_real_aparece_em_tres_frames_de_loop(worker):
    """O numero que explica o bug de churn.

    No regime permanente, tres iteracoes consecutivas do loop carregam UMA
    inferencia nova. Como `AlertStateService` conta iteracoes,
    `create_after_frames=3` e satisfeito por UMA deteccao — e nao por tres,
    como o `.env.example` documenta.

    A primeira iteracao de todas e excecao: sem cache, ela infere. Por isso o
    teste aquece antes de medir.
    """
    for _ in range(3):  # aquecimento: sai do estado "sem cache"
        worker._analyze_frame(FRAME)
    worker.detector.chamadas = 0

    analises = [worker._analyze_frame(FRAME) for _ in range(3)]

    assert worker.detector.chamadas == 1, (
        f"tres iteracoes deveriam carregar UMA inferencia nova; houve {worker.detector.chamadas}"
    )
    assert len({id(a) for a in analises}) == 2, (
        "duas iteracoes reaproveitam a analise anterior e a terceira traz a nova"
    )


def test_sem_intercalacao_toda_iteracao_infere(worker):
    """Contraprova: com detect_every_n=1 nao ha reaproveitamento."""
    worker.detect_every_n = 1
    worker._detect_counter = 0
    worker._cached_analysis = None

    for _ in range(4):
        worker._analyze_frame(FRAME)

    assert worker.detector.chamadas == 4


def test_modelo_unico_preserva_identidade_ao_mudar_ordem_das_pessoas(worker):
    worker.detect_every_n = 1
    esquerda = Detection("person", 0.9, BoundingBox(10, 10, 110, 310), category="person")
    direita = Detection("person", 0.9, BoundingBox(210, 10, 310, 310), category="person")
    worker.detector._deteccoes = [esquerda, direita]
    primeira = worker._analyze_frame(FRAME)
    ids = {item.box.x1: item.track_id for item in primeira.detections}
    assert None not in ids.values()
    worker.detector._deteccoes = [direita, esquerda]
    segunda = worker._analyze_frame(FRAME)
    assert {item.box.x1: item.track_id for item in segunda.detections} == ids
    assert worker.person_detector.chamadas == 0


def test_fps_runtime_muda_o_worker_sem_afetar_config_de_outras_cameras(worker, monkeypatch):
    monkeypatch.setattr(worker, "_emit_timeline_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "_emitir", lambda *args: None)
    worker.app.config["TARGET_FPS"] = 12
    result = worker.update_settings({"target_fps": 30})
    assert result["target_fps"] == 30
    assert worker.target_fps == 30
    assert worker.app.config["TARGET_FPS"] == 12


def test_settings_informa_fps_da_camera(worker):
    worker.target_fps = 24
    assert worker.settings()["target_fps"] == 24
