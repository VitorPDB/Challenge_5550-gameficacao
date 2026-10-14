from __future__ import annotations

import logging
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from app.api.evidence import serve_alert_evidence
from app.models import ROLE_OPERATOR, ROLE_SUPERVISOR, ROLE_TECHNICAL
from app.repositories.alert_repository import AlertRepository
from app.repositories.event_repository import EventRepository
from app.utils.auth import (
    _operador_sem_setor,
    camera_permitida,
    camera_scope,
    erro_fora_do_escopo,
    login_required,
    require_role,
)
from app.utils.salas import emitir_para_camera

logger = logging.getLogger(__name__)


def _camera_filtrada() -> int | None:
    """A camera pedida na query, limitada ao setor de quem esta pedindo.

    O escopo VENCE o parametro: um Operador que passe ?camera_id=99 continua
    vendo so o proprio setor.
    """
    escopo = camera_scope()
    if escopo is not None:
        return escopo
    return request.args.get("camera_id", type=int)

alerts_bp = Blueprint("alerts", __name__)


@alerts_bp.get("/alerts")
@login_required
def list_alerts():
    try:
        limit = min(int(request.args.get("limit", 100)), 500)
    except ValueError:
        limit = 100
    severity = request.args.get("severity")
    status = request.args.get("status")
    feature = request.args.get("feature") or None
    false_positive_raw = request.args.get("false_positive")
    false_positive = None
    if false_positive_raw is not None and false_positive_raw != "":
        false_positive = false_positive_raw.lower() in {"1", "true", "yes", "on"}
    if status and status not in {"active", "resolved"}:
        return jsonify({"error": "status deve ser 'active' ou 'resolved'"}), 400
    if _operador_sem_setor():
        # Sem setor atribuido, nao ve alerta nenhum. Cair no filtro None
        # entregaria o parque INTEIRO — exatamente o oposto da intencao.
        return jsonify({"items": [], "count": 0})

    alerts = AlertRepository().list_recent(
        limit=limit,
        severity=severity,
        status=status,
        false_positive=false_positive,
        camera_id=_camera_filtrada(),
        feature=feature,
    )
    return jsonify({"items": [item.to_dict() for item in alerts], "count": len(alerts)})


@alerts_bp.get("/alerts/<int:alert_id>/evidence")
@login_required
def get_alert_evidence(alert_id: int):
    alert = AlertRepository().get(alert_id)
    if alert is None:
        return jsonify({"error": "alerta não encontrado"}), 404
    return serve_alert_evidence(alert)


@alerts_bp.post("/alerts/<int:alert_id>/false-positive")
@require_role(ROLE_OPERATOR)
def mark_false_positive(alert_id: int):
    payload = request.get_json(silent=True) or {}
    repository = AlertRepository()
    alert = repository.get(alert_id)
    if alert is None:
        return jsonify({"error": "alerta não encontrado"}), 404
    if not camera_permitida(alert.camera_id):
        return erro_fora_do_escopo()
    reason = str(payload.get("reason", "")).strip() or None
    alert = repository.mark_false_positive(alert, reason=reason)
    alert_payload = alert.to_dict()

    event = _log_and_broadcast(
        alert,
        alert_payload,
        message=f"Alerta resolvido como falso positivo: {alert.message}",
        metadata={"reason": reason, "false_positive": True},
        broadcast_resolved=True,
    )
    return jsonify({"alert": alert_payload, "event": event})


@alerts_bp.post("/alerts/<int:alert_id>/acknowledge")
@require_role(ROLE_OPERATOR)
def acknowledge_alert(alert_id: int):
    """Operador confirma que tratou o alerta em campo ("avisei o colaborador").

    Não resolve o alerta — quem resolve é a detecção parar de ver a violação
    (AlertStateService). Isto só registra na linha do tempo QUEM agiu e QUANDO,
    que é o que a auditoria precisa. Antes o botão correspondente no kiosk era
    um window.alert() dizendo "mock, ainda não persiste".
    """
    payload = request.get_json(silent=True) or {}
    repository = AlertRepository()
    alert = repository.get(alert_id)
    if alert is None:
        return jsonify({"error": "alerta não encontrado"}), 404
    if not camera_permitida(alert.camera_id):
        return erro_fora_do_escopo()

    note = str(payload.get("note", "")).strip()[:200] or None
    alert = repository.acknowledge(alert, note=note)
    alert_payload = alert.to_dict()

    event = _log_and_broadcast(
        alert,
        alert_payload,
        message=f"Colaborador avisado: {alert.message}",
        metadata={"acknowledged": True, "note": note},
        broadcast_resolved=False,
    )
    return jsonify({"alert": alert_payload, "event": event})


@alerts_bp.post("/alerts/acknowledge-all")
@require_role(ROLE_OPERATOR)
def acknowledge_all_alerts():
    """"Avisei todos": marca como tratados os alertas ativos ainda nao tratados.

    O Operador so alcanca o proprio setor (`_camera_filtrada` deixa o escopo
    vencer o parametro).
    """
    if _operador_sem_setor():
        return erro_fora_do_escopo()
    payload = request.get_json(silent=True) or {}
    note = str(payload.get("note", "")).strip()[:200] or None
    camera_id = _camera_filtrada()
    total = AlertRepository().acknowledge_all_active(camera_id=camera_id, note=note)
    _registrar_evento_em_lote("alerts_acknowledged_all", f"{total} alerta(s) marcados como tratados", camera_id, total)
    return jsonify({"acknowledged": total})


@alerts_bp.post("/alerts/resolve-active")
@require_role(ROLE_TECHNICAL)
def resolve_active_alerts():
    """Encerra os alertas ativos (nao apaga nada: viram "resolvidos").

    Se a violacao continua acontecendo, o alerta volta em poucos frames — isto
    limpa a fila, nao esconde o problema.
    """
    camera_id = _camera_filtrada()
    ativos_antes = len(AlertRepository().list_active(limit=1000, camera_id=camera_id))
    monitor = current_app.extensions["monitor_service"]
    resultado = monitor.limpar_alertas_ativos(camera_id=camera_id)
    silencio = float(current_app.config.get("ALERT_SNOOZE_AFTER_CLEAR_S", 60.0))
    return jsonify({"active_before": ativos_antes, "silencio_s": silencio, **resultado})


@alerts_bp.delete("/alerts/resolved")
@require_role(ROLE_SUPERVISOR)
def delete_resolved_alerts():
    """Apaga o historico de alertas JA RESOLVIDOS. Ativo nunca e apagado.

    Alerta e registro de auditoria de seguranca do trabalho, por isso: so
    Supervisor, so resolvido, e fica um evento na linha do tempo dizendo quem
    apagou quantos.
    """
    camera_id = _camera_filtrada()
    older = request.args.get("older_than_days", type=int)
    if older is not None and older < 0:
        return jsonify({"error": "older_than_days deve ser >= 0"}), 400
    apagados, arquivos = AlertRepository().delete_resolved(camera_id=camera_id, older_than_days=older)
    removidos = _remover_evidencias(arquivos)
    _registrar_evento_em_lote(
        "alerts_deleted",
        f"{apagados} alerta(s) resolvidos apagados",
        camera_id,
        apagados,
        extra={"older_than_days": older, "evidencias_removidas": removidos},
    )
    return jsonify({"deleted": apagados, "evidence_files_removed": removidos})


def _remover_evidencias(nomes: list[str]) -> int:
    """Remove snapshots que nenhum alerta restante referencia."""
    monitor = current_app.extensions.get("monitor_service")
    try:
        servico = monitor.snapshot_service
    except (LookupError, AttributeError):  # sem camera padrao / monitor de teste
        return 0
    pasta = Path(servico.absolute_dir)
    removidos = 0
    for nome in nomes:
        alvo = pasta / Path(nome).name  # .name: nunca sai da pasta de evidencias
        try:
            if alvo.is_file():
                alvo.unlink()
                removidos += 1
        except OSError as exc:
            logger.warning("evidencia_nao_removida", extra={"arquivo": nome, "error": str(exc)})
    return removidos


def _registrar_evento_em_lote(tipo: str, mensagem: str, camera_id, total: int, extra: dict | None = None) -> None:
    from app.utils.auth import current_user

    usuario = current_user()
    try:
        EventRepository().create(
            event_type=tipo,
            severity="info",
            message=mensagem,
            camera_id=camera_id,
            metadata={"total": total, "por": getattr(usuario, "email", None), **(extra or {})},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("evento_em_lote_nao_gravado", extra={"tipo": tipo, "error": str(exc)})


def _subject_of(alert) -> str | None:
    metadata = alert.metadata_json or {}
    return metadata.get("person_label") or metadata.get("person_id") or alert.feature


def _log_and_broadcast(alert, alert_payload: dict, *, message: str, metadata: dict, broadcast_resolved: bool) -> dict | None:
    """Grava o evento de timeline e avisa os clientes conectados.

    Falha aqui não pode derrubar a ação do operador (o alerta JÁ foi gravado no
    banco), mas também não pode sumir sem rastro como sumia antes num
    `except: pass`.
    """
    monitor = current_app.extensions.get("monitor_service")
    if monitor is not None and hasattr(monitor, "update_alert_snapshot"):
        monitor.update_alert_snapshot(alert_payload)
    socketio = getattr(monitor, "socketio", None) or current_app.extensions.get("socketio")
    try:
        if broadcast_resolved:
            event = EventRepository().create_alert_resolved_once(
                alert_payload=alert_payload,
                severity="info",
                message=message,
                subject=_subject_of(alert),
                metadata=metadata,
            )
        else:
            event = EventRepository().create(
                event_type="alert_acknowledged",
                severity="info",
                message=message,
                subject=_subject_of(alert),
                camera_id=alert.camera_id,
                metadata=metadata | {"alert_id": str(alert.id), "alert": alert_payload},
            )
        event_payload = event.to_dict() if event is not None else None

        if socketio is not None:
            # Escopo de camera tambem aqui: o operador de um setor nao pode
            # receber pelo socket o alerta que a rota REST lhe negaria.
            # Ver app/utils/salas.py e tests/test_escopo_socket.py.
            if event_payload is not None:
                emitir_para_camera(socketio, "timeline_event", event_payload, alert.camera_id)
            if broadcast_resolved:
                emitir_para_camera(socketio, "alert_resolved", alert_payload, alert.camera_id)
                items = [item.to_dict() for item in AlertRepository().list_active_for_camera(alert.camera_id)]
                emitir_para_camera(
                    socketio,
                    "active_alerts",
                    {"camera_id": alert.camera_id, "items": items, "count": len(items)},
                    alert.camera_id,
                )
            else:
                emitir_para_camera(socketio, "alert_updated", alert_payload, alert.camera_id)
        return event_payload
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert_action_broadcast_failed", extra={"alert_id": alert.id, "error": str(exc)})
        return None
