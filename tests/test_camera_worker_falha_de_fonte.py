"""Caracterização do `_loop` do `CameraWorker` quando a FONTE FALHA.

`camera_worker.py` está a 34% de cobertura e o loop de frame não é exercitado
por nenhum teste — mesma situação que motivou `test_camera_worker_cache.py`,
e o mesmo procedimento: documentar o comportamento ATUAL antes de mexer.

O caminho de falha é o que vai rodar na sexta. **Não há rota desta máquina para
a rede da planta** (10.14.0.0/16): os 5 endereços dão timeout, e o RTSP real
nunca foi exercitado aqui. Então o comportamento sob fonte morta não é um caso
de borda — é o caminho principal do demo.

O que este arquivo trava, na ordem em que os testes aparecem:

1. Fonte que nunca abre não publica frame nenhum, e o loop **não morre**.
2. O estado percorre `reconnecting` → `unavailable` conforme o backoff
   satura — isso JÁ existe em `VideoStream` (retry com backoff e teto,
   `app/vision/video_stream.py`) e não precisa ser construído.
3. O backoff é respeitado: uma fonte morta custa uma tentativa por janela, não
   uma por iteração de loop.
4. Exceção dentro da análise não derruba o loop nem a thread — uma câmera
   quebrada não pode levar as outras.
5. **A LACUNA:** não existe modo fixture. `status()` sabe dizer "reconectando"
   e "indisponível", mas não sabe dizer "estou numa fonte de demonstração".
   Hoje uma câmera morta fica morta para sempre, e o dashboard mostra
   "reconectando" indefinidamente. Os dois últimos testes falham de propósito.

Nada aqui abre câmera nem carrega peso: os detectores são dublês injetados no
construtor, e a fonte é um dublê que devolve falha.
"""

from __future__ import annotations

import pathlib
import threading

import numpy as np
import pytest
from app import create_app
from app.config import TestConfig
from app.extensions import db
from app.services.camera_worker import CameraWorker
from app.services.feature_manager import FeatureManager
from app.vision.schemas import PoseResult
from app.vision.video_stream import RECONNECTING, UNAVAILABLE, StreamStatus

RAIZ = pathlib.Path(__file__).resolve().parent.parent
FRAME = np.zeros((540, 960, 3), dtype=np.uint8)


class SocketDuble:
    def __init__(self) -> None:
        self.eventos: list[tuple[str, object]] = []

    def emit(self, evento, payload=None, *args, **kwargs):  # noqa: ARG002
        self.eventos.append((evento, payload))

    def start_background_task(self, target, *args, **kwargs):  # noqa: ARG002
        return None

    def eventos_de(self, nome: str) -> list[object]:
        return [payload for evento, payload in self.eventos if evento == nome]


class DetectorDuble:
    model_path = "duble.pt"
    confidence = 0.35
    max_detections = 100

    def __init__(self) -> None:
        self.chamadas = 0

    def detect(self, frame):  # noqa: ARG002
        self.chamadas += 1
        return []

    def diagnostics(self):
        return {"model_path": "duble.pt", "classes": {}, "error": None}

    def supported_ppe_classes(self):
        return {"helmet", "vest"}


class PoseDuble:
    def estimate(self, frame):  # noqa: ARG002
        return PoseResult(landmarks=[])

    def estimate_for_people(self, frame, people, *, max_people=4):  # noqa: ARG002
        return []


class FonteMorta:
    """Dublê de `VideoStream` que nunca entrega frame.

    Reproduz o caso clássico do RTSP: o `VideoCapture` fica "aberto" e todo
    `read()` devolve `False`. Conta as tentativas para o teste de backoff.
    """

    def __init__(self, *, source: str = "rtsp://fonte-morta:554/x") -> None:
        self.source = source
        self.width, self.height = 960, 540
        self.leituras = 0
        self.liberado = 0
        self._estado = RECONNECTING
        self._tentativas = 0

    def read(self):
        self.leituras += 1
        self._tentativas += 1
        # Satura no teto depois de algumas tentativas, como o backoff real.
        if self._tentativas > 3:
            self._estado = UNAVAILABLE
        return False, None

    def status(self) -> StreamStatus:
        return StreamStatus(
            state=self._estado,
            consecutive_failures=self.leituras,
            reconnect_attempts=self._tentativas,
            total_reconnects=0,
            last_error="Frame indisponível",
            seconds_until_retry=0.0,
        )

    def release(self) -> None:
        self.liberado += 1

    def latest_frame(self):
        return None


class RodarAte:
    """Substitui o `threading.Event` do `_running` para limitar as iterações.

    Sem isto, exercitar `_loop` exigiria thread e `sleep` — não determinístico,
    e um teste de loop infinito que depende de tempo é um teste instável.

    `is_set()` responde a um PREDICADO, e não a uma contagem de chamadas. Contar
    chamadas seria frágil, e a descoberta é parte da caracterização: `status()`
    também chama `_running.is_set()` (é o campo `running`), e
    `_handle_capture_failure` chama `status()` a cada transição de estado — ou
    seja, o número de iteracões dependeria de quantas transições ocorreram.
    """

    def __init__(self, continuar) -> None:
        self._continuar = continuar

    def is_set(self) -> bool:
        return bool(self._continuar())

    def set(self) -> None: ...

    def clear(self) -> None:
        self._continuar = lambda: False


def montar_worker(app, *, socket: SocketDuble, camera_id: int = 1) -> CameraWorker:
    return CameraWorker(
        app,
        socketio=socket,
        feature_manager=FeatureManager.from_config(app.config),
        camera_id=camera_id,
        source="rtsp://fonte-morta:554/x",
        fps=12,
        detector=DetectorDuble(),
        person_detector=DetectorDuble(),
        pose_estimator=PoseDuble(),
        inference_lock=threading.Lock(),
    )


def _worker_de_teste(monkeypatch, config):
    app = create_app(config)
    socket = SocketDuble()
    with app.app_context():
        db.create_all()
        trabalhador = montar_worker(app, socket=socket)
        trabalhador.video_stream = FonteMorta()
        # `_handle_capture_failure` dorme entre 0,2 s e 1 s por iteração. Sem
        # neutralizar, um teste de 5 iterações levaria 1 s por nada.
        dormidas: list[float] = []
        monkeypatch.setattr("app.services.camera_worker.time.sleep", dormidas.append)
        trabalhador.dormidas = dormidas
        trabalhador.socket_duble = socket
        yield trabalhador
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def worker(monkeypatch):
    """Sem fonte de reserva: ISOLA o caminho de reconexão.

    Com a reserva ligada, o worker troca de fonte no teto de tentativas e os
    testes de backoff deixariam de exercitar o que dizem exercitar.
    """

    class SemReserva(TestConfig):
        RTSP_FIXTURE_FALLBACK = ""

    yield from _worker_de_teste(monkeypatch, SemReserva)


@pytest.fixture()
def worker_com_reserva(monkeypatch, tmp_path):
    """Decodifica um vídeo local de verdade sem depender de download externo."""
    import cv2

    fixture = tmp_path / "bench.mp4"
    writer = cv2.VideoWriter(str(fixture), cv2.VideoWriter_fourcc(*"mp4v"), 12, (960, 540))
    assert writer.isOpened(), "codec mp4v indisponível para criar a fixture de teste"
    try:
        for _ in range(12):
            writer.write(np.full((540, 960, 3), 80, dtype=np.uint8))
    finally:
        writer.release()

    class ComReserva(TestConfig):
        RTSP_FIXTURE_FALLBACK = str(fixture)
        RTSP_MAX_TENTATIVAS = 5

    yield from _worker_de_teste(monkeypatch, ComReserva)


def rodar_loop(worker, iteracoes: int) -> None:
    """Roda `_loop` até a fonte ter sido lida `iteracoes` vezes.

    Para na hora se o worker TROCAR de fonte: a fonte nova é um `VideoStream`
    de verdade, sem o contador do dublê. Quem quer exercitar a fonte nova usa
    `rodar_ate_frames`.
    """
    fonte = worker.video_stream
    alvo = fonte.leituras + iteracoes
    worker._running = RodarAte(
        lambda: worker.video_stream is fonte and worker.video_stream.leituras < alvo
    )
    worker._loop()


def rodar_ate_frames(worker, quantos: int) -> None:
    """Roda `_loop` até PUBLICAR `quantos` frames novos."""
    alvo = worker._frame_counter + quantos
    tentativas = 0

    def continuar():
        nonlocal tentativas
        tentativas += 1
        assert tentativas <= 100, f"o loop não publicou {quantos} frames; último erro: {worker._last_error}"
        return worker._frame_counter < alvo

    worker._running = RodarAte(continuar)
    worker._loop()


# --------------------------------------------- 1. fonte morta não publica --
def test_fonte_morta_nao_publica_frame_e_o_loop_sobrevive(worker):
    rodar_loop(worker, 5)

    assert worker.video_stream.leituras == 5, "toda iteração deveria tentar ler"
    assert worker.latest_jpeg() is None, "não pode publicar frame sem frame"
    assert worker.status()["frame_counter"] == 0
    assert worker.detector.chamadas == 0, "sem frame, não roda inferência"


def test_fonte_morta_registra_o_erro_no_status(worker):
    rodar_loop(worker, 3)

    assert worker.status()["last_error"] == "Frame indisponível"


# --------------------------------- 2. backoff e teto: JÁ EXISTEM hoje ------
def test_estado_percorre_reconnecting_e_depois_unavailable(worker):
    """Retry com backoff e teto já está construído em `VideoStream`.

    Este teste existe para provar que está construído — não para pedir que
    seja. Ver `app/vision/video_stream.py::_schedule_retry`.
    """
    rodar_loop(worker, 2)
    assert worker.status()["video"]["state"] == RECONNECTING

    rodar_loop(worker, 4)
    assert worker.status()["video"]["state"] == UNAVAILABLE


def test_transicao_de_estado_emite_monitor_status(worker):
    """A ~12 FPS, emitir por frame ruim seriam 12 eventos por segundo.

    `emitir_para_camera` manda 2 vezes por evento (sala do parque + sala da
    câmera, ver app/utils/salas.py), então 2 transições = 4 emissões.
    """
    rodar_loop(worker, 6)

    assert len(worker.socket_duble.eventos_de("monitor_status")) == 4, (
        f"esperava 2 transições x 2 salas; houve {len(worker.socket_duble.eventos_de('monitor_status'))}"
    )


def test_o_loop_dorme_o_backoff_em_vez_de_girar_apertado(worker):
    """Fonte morta custa uma tentativa por janela, não uma por iteração."""
    rodar_loop(worker, 4)

    assert worker.dormidas, "sem sleep, uma fonte morta martelaria o dispositivo"
    assert all(0.0 <= s <= 1.0 for s in worker.dormidas), worker.dormidas


# ----------------------- 3. uma câmera quebrada não derruba o processo -----
def test_excecao_na_analise_nao_derruba_o_loop(worker, monkeypatch):
    """Requisito: uma câmera morta não derruba as outras nem o processo.

    Aqui a fonte ENTREGA frame e a análise explode — o outro modo de falha.
    """

    class FonteQueEntrega(FonteMorta):
        def read(self):
            self.leituras += 1
            return True, FRAME

    worker.video_stream = FonteQueEntrega()

    def explodir(_frame):
        raise RuntimeError("modelo caiu no meio da inferência")

    monkeypatch.setattr(worker, "_analyze_frame", explodir)

    rodar_loop(worker, 3)  # não deve levantar

    assert worker.video_stream.leituras == 3, "o loop continuou tentando"
    assert worker.status()["last_error"] == "modelo caiu no meio da inferência"


def test_status_de_camera_morta_continua_respondendo(worker):
    """`status()` é o que o dashboard lê. Se ele levantar, a UI fica cega."""
    rodar_loop(worker, 4)

    estado = worker.status()
    assert estado["running"] is False
    assert estado["video"]["state"] in (RECONNECTING, UNAVAILABLE)


# -------------------------- 4. A LACUNA: não existe modo fixture ----------
def test_status_expoe_modo_de_fonte(worker):
    """`status()` precisa dizer QUAL fonte está em uso, não só se caiu.

    Decisão do usuário: modo fixture é estado de PRIMEIRA CLASSE na UI, não
    degradação discreta. O avaliador tem que ler "fallback deliberado", não
    "quebrado". Para isso o dashboard precisa de um campo, e hoje não existe:
    `status()["video"]` só tem state/failures/attempts/reconnects/last_error.
    """
    rodar_loop(worker, 6)

    video = worker.status()["video"]
    assert "modo" in video, f"status()['video'] não expõe 'modo'; tem apenas {sorted(video)}"
    assert video["modo"] == "reconectando", "fonte morta, sem reserva: o modo é reconectando"


def test_apos_o_teto_de_tentativas_assume_a_fixture(worker_com_reserva):
    """Depois de N tentativas o worker adota a fixture em loop.

    Sem isto, uma câmera morta fica morta para sempre e o demo não tem imagem.
    Com isto, o demo continua — e diz que continua com fonte de demonstração.
    """
    worker = worker_com_reserva
    rodar_loop(worker, 40)  # para sozinho na troca de fonte

    video = worker.status()["video"]
    assert video["modo"] == "fixture", f"esperava modo fixture; está {video}"
    assert str(worker.video_stream.source).endswith("bench.mp4"), worker.video_stream.source
    assert worker.video_stream.em_loop is True, "a fixture tem que reiniciar ao terminar"
    assert worker.fonte_configurada == "rtsp://fonte-morta:554/x", (
        "a fonte CONFIGURADA não pode ser sobrescrita: é ela que MonitorService "
        "compara com o banco para decidir se a câmera foi editada"
    )


def test_em_modo_fixture_a_camera_volta_a_entregar_imagem(worker_com_reserva):
    """O ponto do fallback: o demo CONTINUA.

    Não basta trocar o rótulo — a câmera tem que voltar a publicar frame de
    verdade. Aqui a fixture real é decodificada pelo `_loop`.
    """
    worker = worker_com_reserva
    rodar_loop(worker, 40)
    assert worker.latest_jpeg() is None, "antes da troca não havia imagem"

    rodar_ate_frames(worker, 3)

    assert worker.latest_jpeg() is not None, "modo fixture sem imagem não serve de nada"
    assert worker._frame_counter >= 3
    assert worker.status()["video"]["state"] == "live"
    assert worker.status()["video"]["modo"] == "fixture", (
        "entregando frame, mas de fonte de demonstração: os dois ao mesmo tempo"
    )


def test_a_troca_para_fixture_e_anunciada_no_socket(worker_com_reserva):
    """Requisito: sinaliza modo fixture no `status()` E no payload de socket."""
    worker = worker_com_reserva
    rodar_loop(worker, 40)

    payloads = worker.socket_duble.eventos_de("monitor_status")
    assert payloads, "a troca de fonte tem que ser anunciada"
    assert any(p["video"]["modo"] == "fixture" for p in payloads), (
        f"nenhum monitor_status anunciou modo fixture: {[p['video']['modo'] for p in payloads]}"
    )


def test_sem_a_fixture_no_disco_nao_mente_modo_fixture(worker_com_reserva):
    """A fixture não é versionada (docs/FIXTURES.md): pode não estar no disco.

    Dizer "modo fixture" sem ter fixture seria pior que ficar em reconectando:
    a tela afirmaria que há imagem de demonstração e não haveria nenhuma.
    """
    worker = worker_com_reserva
    worker.fonte_reserva = str(RAIZ / "tests" / "fixtures" / "nao-existe.mp4")

    rodar_loop(worker, 40)

    assert worker.status()["video"]["modo"] == "reconectando"
    assert worker._reserva_indisponivel is True
