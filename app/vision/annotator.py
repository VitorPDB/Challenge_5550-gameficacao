from __future__ import annotations

from typing import Any, Callable

import cv2
import numpy as np

from app.vision.schemas import Detection, PoseResult

# Cores em BGR (OpenCV), nao RGB. Tres matizes codificando CONFORMIDADE, ja
# que a classe esta escrita no rotulo: branco para pessoa, verde para EPI
# presente, laranja para perigo (EPI ausente, queda, area de risco).
COR_PESSOA = (255, 255, 255)
COR_OK = (138, 209, 72)
COR_PERIGO = (94, 158, 255)
COR_ROTULO_FUNDO = (22, 19, 18)
COR_ROTULO_TEXTO = (247, 245, 245)

# Tracos. OpenCV so aceita espessura inteira (cv2.line/cv2.rectangle:
# `thickness` e int, https://docs.opencv.org/4.x/d6/d6e/group__imgproc__draw.html),
# entao 1,8 px e 1,6 px arredondam para 2 e 1,5 px para 2 no raio do ponto.
TRACO_PESSOA = 2
TRACO_OK = 2
TRACO_PERIGO = 3
TRACO_AREA = 2
RAIO_CAIXA = 10
RAIO_ROTULO = 9
FONTE = cv2.FONT_HERSHEY_SIMPLEX
ESCALA_FONTE = 0.5


def _retangulo_arredondado(
    frame: np.ndarray,
    p1: tuple[int, int],
    p2: tuple[int, int],
    cor: tuple[int, int, int],
    espessura: int,
    raio: int,
) -> None:
    """Retangulo de cantos arredondados: quatro linhas e quatro elipses.
    `espessura` negativa preenche, como no cv2.rectangle."""
    x1, x2 = sorted((int(p1[0]), int(p2[0])))
    y1, y2 = sorted((int(p1[1]), int(p2[1])))
    r = max(0, min(int(raio), (x2 - x1) // 2, (y2 - y1) // 2))
    if r == 0:
        cv2.rectangle(frame, (x1, y1), (x2, y2), cor, espessura)
        return
    if espessura < 0:
        cv2.rectangle(frame, (x1 + r, y1), (x2 - r, y2), cor, -1)
        cv2.rectangle(frame, (x1, y1 + r), (x2, y2 - r), cor, -1)
    else:
        cv2.line(frame, (x1 + r, y1), (x2 - r, y1), cor, espessura)
        cv2.line(frame, (x1 + r, y2), (x2 - r, y2), cor, espessura)
        cv2.line(frame, (x1, y1 + r), (x1, y2 - r), cor, espessura)
        cv2.line(frame, (x2, y1 + r), (x2, y2 - r), cor, espessura)
    # Angulos no sentido horario a partir de +x, como o OpenCV mede em imagem.
    cv2.ellipse(frame, (x1 + r, y1 + r), (r, r), 180, 0, 90, cor, espessura)
    cv2.ellipse(frame, (x2 - r, y1 + r), (r, r), 270, 0, 90, cor, espessura)
    cv2.ellipse(frame, (x2 - r, y2 - r), (r, r), 0, 0, 90, cor, espessura)
    cv2.ellipse(frame, (x1 + r, y2 - r), (r, r), 90, 0, 90, cor, espessura)


def _com_opacidade(frame: np.ndarray, alpha: float, desenhar: Callable[[np.ndarray], None]) -> None:
    """Desenha numa copia e mistura de volta com `alpha`. Uma copia por
    camada, nao por elemento: agrupe o que compartilha a mesma opacidade."""
    camada = frame.copy()
    desenhar(camada)
    cv2.addWeighted(camada, alpha, frame, 1 - alpha, 0, dst=frame)


class FrameAnnotator:
    def __init__(self, risk_polygon: list[tuple[float, float]]) -> None:
        self.risk_polygon = risk_polygon

    def annotate(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        poses: list[PoseResult] | None,
        enabled_features: dict[str, bool],
        compliance_state: dict[str, Any] | None = None,
        overlay_options: dict[str, bool] | None = None,
    ) -> np.ndarray:
        output = frame.copy()
        options = overlay_options or {}
        if options.get("risk_area", True):
            self._draw_risk_area(output, enabled_features, options)
        self._draw_detections(output, detections, options, compliance_state)
        if enabled_features.get("pose", False) and options.get("pose", True):
            encontradas = [pose for pose in poses or [] if pose and pose.found]
            if encontradas:
                _com_opacidade(output, 0.5, lambda camada: [self._draw_pose_points(camada, pose) for pose in encontradas])
        return output

    # Rotulo do SH17 -> parte do corpo controlada por overlay `part_<parte>`.
    # "ear-acessorio" NAO entra aqui: e o protetor auricular (EPI de verdade,
    # ver yolo_ppe_detector.PPE_CLASS_ALIASES), desenhado sempre, como
    # qualquer outro EPI — nao e rotulo de contexto que o operador liga/desliga.
    _PARTES = {
        "head": "head",
        "face": "face",
        "face-acessorio": "face",
        "ear": "ear",
        "hands": "hands",
        "foot": "foot",
        "tool": "tool",
    }

    @staticmethod
    def _caixas_sem_epi(compliance_state: dict[str, Any] | None) -> set[tuple[int, int, int, int]]:
        """Caixas das pessoas com algum EPI `missing` OU `incorrect` no estado
        de conformidade. E isso que decide "EPI ausente/incorreto": a deteccao
        sozinha so sabe o que ESTA no frame, nunca o que falta ou esta errado."""
        caixas: set[tuple[int, int, int, int]] = set()
        for pessoa in (compliance_state or {}).get("people", []) or []:
            ppe = pessoa.get("ppe") or {}
            if any((item or {}).get("status") in ("missing", "incorrect") for item in ppe.values()):
                box = pessoa.get("box") or {}
                try:
                    caixas.add((int(box["x1"]), int(box["y1"]), int(box["x2"]), int(box["y2"])))
                except (KeyError, TypeError, ValueError):
                    continue
        return caixas

    def _draw_detections(
        self,
        frame: np.ndarray,
        detections: list[Detection],
        options: dict[str, bool],
        compliance_state: dict[str, Any] | None,
    ) -> None:
        show_boxes = options.get("boxes", True)
        show_labels = options.get("labels", True)
        show_confidence = options.get("confidence", True)
        if not show_boxes and not show_labels:
            return

        # Rotulos anatomicos do SH17 (head/face/ear/hands/foot): so servem de
        # contexto, entao ficam escondidos a menos que o operador ligue.
        detections = [
            d for d in detections
            if d.label.lower() not in self._PARTES or options.get(f"part_{self._PARTES[d.label.lower()]}", False)
        ]

        sem_epi = self._caixas_sem_epi(compliance_state)
        conformes: list[Detection] = []
        for det in detections:
            e_pessoa = det.label == "person" or det.category == "person"
            box = (det.box.x1, det.box.y1, det.box.x2, det.box.y2)
            if e_pessoa and box in sem_epi:
                cor, traco = COR_PERIGO, TRACO_PERIGO
            elif e_pessoa:
                conformes.append(det)
                continue
            elif det.label == "fall_detected":
                cor, traco = COR_PERIGO, TRACO_PERIGO
            else:
                cor, traco = COR_OK, TRACO_OK
            if show_boxes:
                _retangulo_arredondado(frame, (det.box.x1, det.box.y1), (det.box.x2, det.box.y2), cor, traco, RAIO_CAIXA)

        # Pessoa conforme: branco a 90%, numa camada so pra todas.
        if show_boxes and conformes:
            _com_opacidade(
                frame,
                0.9,
                lambda camada: [
                    _retangulo_arredondado(camada, (d.box.x1, d.box.y1), (d.box.x2, d.box.y2), COR_PESSOA, TRACO_PESSOA, RAIO_CAIXA)
                    for d in conformes
                ],
            )

        if show_labels:
            rotulos = []
            for det in detections:
                e_pessoa = det.label == "person" or det.category == "person"
                # Pessoa leva o MESMO numero dos alertas ("Pessoa 8"), pra dar pra
                # ligar a caixa ao alerta. Sem track_id cai no rotulo generico.
                if e_pessoa and det.track_id is not None:
                    text = f"Pessoa {det.track_id}"
                else:
                    text = self._display_label(det.label)
                if show_confidence:
                    text = f"{text} {det.confidence:.2f}"
                # Pessoa: pilula acima da caixa. EPI: dentro da propria caixa,
                # senao colide com a pilula da pessoa quando os topos coincidem.
                rotulos.append((text, det.box.x1, det.box.y1, not e_pessoa))
            self._draw_labels(frame, rotulos)

    @staticmethod
    def _draw_labels(frame: np.ndarray, rotulos: list[tuple[str, int, int, bool]]) -> None:
        """Todo rotulo vai sobre uma pilula escura a 75%, encostada no canto
        superior esquerdo da caixa (acima dela, ou dentro quando `dentro`).
        cv2.putText com Hershey sobre imagem clara e ilegivel e nenhuma cor
        de texto conserta isso."""
        if not rotulos:
            return
        h, w = frame.shape[:2]
        pilulas: list[tuple[tuple[int, int], tuple[int, int]]] = []
        textos: list[tuple[str, tuple[int, int]]] = []
        for text, x, y, dentro in rotulos:
            (tw, th), base = cv2.getTextSize(text, FONTE, ESCALA_FONTE, 1)
            alt = th + base + 8
            larg = tw + 12
            px1 = max(0, min(int(x), w - larg))
            # Acima da caixa quando cabe; dentro dela quando pedido ou no topo do frame.
            py1 = int(y) if dentro or int(y) - alt < 0 else int(y) - alt
            py1 = max(0, min(py1, h - alt))
            pilulas.append(((px1, py1), (px1 + larg, py1 + alt)))
            textos.append((text, (px1 + 6, py1 + alt - base - 4)))

        _com_opacidade(
            frame,
            0.75,
            lambda camada: [_retangulo_arredondado(camada, p1, p2, COR_ROTULO_FUNDO, -1, RAIO_ROTULO) for p1, p2 in pilulas],
        )
        for text, origem in textos:
            cv2.putText(frame, text, origem, FONTE, ESCALA_FONTE, COR_ROTULO_TEXTO, 1, cv2.LINE_AA)

    def _draw_pose_points(self, frame: np.ndarray, pose: PoseResult) -> None:
        h, w = frame.shape[:2]
        for landmark in pose.landmarks:
            if landmark.visibility < 0.45:
                continue
            cv2.circle(frame, (int(landmark.x * w), int(landmark.y * h)), 2, COR_PESSOA, -1, cv2.LINE_AA)

    def _draw_risk_area(self, frame: np.ndarray, enabled_features: dict[str, bool], options: dict[str, bool]) -> None:
        if not enabled_features.get("risk_area", False):
            return
        h, w = frame.shape[:2]
        points = np.array([[int(x * w), int(y * h)] for x, y in self.risk_polygon], dtype=np.int32)
        if len(points) < 3:
            return
        _com_opacidade(frame, 0.08, lambda camada: cv2.fillPoly(camada, [points], COR_PERIGO))
        cv2.polylines(frame, [points], isClosed=True, color=COR_PERIGO, thickness=TRACO_AREA, lineType=cv2.LINE_AA)
        if options.get("labels", True):
            self._draw_labels(frame, [("area de risco", int(points[0][0]), int(points[0][1]), False)])

    # Sem acento de proposito: cv2.putText usa fonte Hershey, que nao tem
    # glifo pra acentuacao; sairia caixinha desenhada no frame.
    _LABELS = {
        "helmet": "capacete",
        "vest": "colete",
        "gloves": "luvas",
        "glasses": "oculos",
        "mask": "mascara",
        "safety_shoe": "calcado",
        "ear_protection": "protetor auricular",
        "safety_cone": "cone",
        "fall_detected": "queda",
        "person": "pessoa",
    }

    @classmethod
    def _display_label(cls, label: str) -> str:
        return cls._LABELS.get(label, label)
