from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services.risk_score_service import compute_risk_score, compute_risk_trend
from app.utils.auth import _operador_sem_setor, camera_scope, erro_fora_do_escopo, login_required

risk_bp = Blueprint("risk", __name__)


def _camera_id() -> int | None:
    """`?camera_id=N` restringe o score a uma câmera. Ausente = todas — é o que
    o painel consolidado do Supervisor quer.

    Para o Operador o escopo do setor VENCE o parâmetro: o risco que ele vê é o
    da área dele, mesmo que peça outra.
    """
    escopo = camera_scope()
    if escopo is not None:
        return escopo
    return request.args.get("camera_id", type=int)


@risk_bp.get("/risk-score")
@login_required
def risk_score():
    if _operador_sem_setor():
        return erro_fora_do_escopo()
    return jsonify(compute_risk_score(camera_id=_camera_id()))


@risk_bp.get("/risk-score/trend")
@login_required
def risk_score_trend():
    if _operador_sem_setor():
        return erro_fora_do_escopo()
    hours = request.args.get("hours", default=24, type=int)
    bucket_hours = request.args.get("bucket_hours", default=1, type=int)
    # Clamp: sparkline não precisa (nem deveria aceitar) baldes fora de uma
    # janela razoável — protege contra ?hours=999999 gerando resposta gigante.
    hours = max(1, min(hours, 24 * 14))
    bucket_hours = max(1, min(bucket_hours, 24))
    return jsonify(compute_risk_trend(hours=hours, bucket_hours=bucket_hours, camera_id=_camera_id()))
