"""Cor do overlay codifica conformidade, nao classe (rebrand, etapa 8)."""

import numpy as np
from app.vision.annotator import COR_OK, COR_PERIGO, FrameAnnotator, _retangulo_arredondado
from app.vision.schemas import BoundingBox, Detection


def _frame() -> np.ndarray:
    return np.zeros((200, 300, 3), dtype=np.uint8)


def _pessoa() -> Detection:
    return Detection(label="person", confidence=0.9, box=BoundingBox(20, 20, 120, 180), category="person")


def _compliance(status: str) -> dict:
    return {"people": [{"box": _pessoa().box.to_dict(), "ppe": {"helmet": {"status": status}}}]}


def test_pessoa_sem_epi_fica_laranja_e_epi_presente_fica_verde():
    capacete = Detection(label="helmet", confidence=0.8, box=BoundingBox(150, 20, 200, 60))
    out = FrameAnnotator([]).annotate(_frame(), [_pessoa(), capacete], None, {}, _compliance("missing"), {"labels": False})
    # Borda esquerda da pessoa, no meio da altura: perigo, opacidade total.
    assert tuple(int(v) for v in out[100, 20]) == COR_PERIGO
    # Borda esquerda do capacete: EPI presente.
    assert tuple(int(v) for v in out[40, 150]) == COR_OK


def test_pessoa_conforme_fica_branca_a_90_por_cento():
    out = FrameAnnotator([]).annotate(_frame(), [_pessoa()], None, {}, _compliance("ok"), {"labels": False})
    px = [int(v) for v in out[100, 20]]
    assert all(220 <= v <= 235 for v in px), px  # 255 * 0,9 sobre preto


def test_retangulo_arredondado_nao_pinta_o_canto():
    frame = _frame()
    _retangulo_arredondado(frame, (10, 10), (100, 100), (255, 255, 255), 2, 10)
    assert frame[10, 10].sum() == 0
    assert frame[10, 55].sum() > 0


def test_rotulo_sobre_pilula_nao_estoura_o_frame():
    perto_da_borda = Detection(label="helmet", confidence=0.8, box=BoundingBox(280, 0, 299, 30))
    out = FrameAnnotator([]).annotate(_frame(), [perto_da_borda], None, {}, None, None)
    assert out.shape == (200, 300, 3)
    assert out.sum() > 0


def test_caixa_da_pessoa_mostra_o_numero_dela(monkeypatch):
    import numpy as np
    from app.vision.annotator import FrameAnnotator
    from app.vision.schemas import BoundingBox, Detection

    vistos = []
    monkeypatch.setattr(FrameAnnotator, "_draw_labels", staticmethod(lambda frame, rotulos: vistos.extend(r[0] for r in rotulos)))
    det = Detection(label="person", confidence=0.76, box=BoundingBox(10, 10, 100, 200), category="person", track_id=8)
    FrameAnnotator(risk_polygon=[]).annotate(np.zeros((240, 320, 3), np.uint8), [det], None, {}, None, {})
    assert vistos == ["Pessoa 8 0.76"]
