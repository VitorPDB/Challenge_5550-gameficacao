from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from app.extensions import db
from app.models import Alert


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Marcas gravadas por acao humana, que a renovacao automatica nao pode apagar.
CHAVES_DE_RECONHECIMENTO = ("acknowledged", "acknowledged_at", "acknowledged_note")


class AlertRepository:
    def create(
        self,
        *,
        rule: str,
        severity: str,
        message: str,
        feature: str | None = None,
        metadata: dict[str, Any] | None = None,
        frame_ref: str | None = None,
        status: str = "active",
        camera_id: int | None = None,
    ) -> Alert:
        now = utc_now()
        alert = Alert(
            camera_id=camera_id,
            rule=rule,
            severity=severity,
            message=message,
            feature=feature,
            metadata_json=metadata or {},
            frame_ref=frame_ref,
            status=status,
            occurrences=1,
            created_at=now,
            first_seen_at=now,
            last_seen_at=now,
        )
        db.session.add(alert)
        db.session.commit()
        return alert

    def get(self, alert_id: int) -> Alert | None:
        return db.session.get(Alert, int(alert_id))

    def get_by_frame_ref(self, frame_ref: str) -> Alert | None:
        return Alert.query.filter_by(frame_ref=frame_ref).first()

    def list_active_for_camera(self, camera_id: int | None) -> list[Alert]:
        return Alert.query.filter_by(camera_id=camera_id, status="active").order_by(Alert.last_seen_at.desc()).all()

    def touch(self, alert: Alert, *, metadata: dict[str, Any] | None = None, incremento: int = 1) -> Alert:
        """Renova o alerta ativo. `incremento` permite gravar de uma vez as
        ocorrências acumuladas desde o último commit — ver
        AlertStateService.intervalo_touch, que espaça esta gravação."""
        db.session.refresh(alert, attribute_names=["status", "metadata_json"])
        if alert.status != "active":
            return alert
        preservadas: dict[str, Any] = {}
        if metadata is not None:
            # O worker regrava a metadata inteira a cada renovacao, mas o
            # "avisei" (unico ou em lote) e gravado por OUTRA thread, na rota
            # HTTP. Sem reler o banco e preservar essas chaves, a marca sumia
            # em ate 2 s: o botao parecia funcionar e o alerta voltava a
            # aparecer como nao tratado. Recarrega SO a coluna de metadata e
            # ANTES de mexer no resto: um refresh completo depois descartaria
            # a contagem de ocorrencias ainda nao gravada.
            db.session.refresh(alert, attribute_names=["metadata_json"])
            atual = alert.metadata_json or {}
            preservadas = {k: atual[k] for k in CHAVES_DE_RECONHECIMENTO if k in atual}
        alert.last_seen_at = utc_now()
        alert.occurrences = int(alert.occurrences or 0) + max(1, int(incremento))
        if metadata is not None:
            alert.metadata_json = {**metadata, **preservadas}
        db.session.commit()
        return alert

    def resolve(self, alert: Alert, *, metadata: dict[str, Any] | None = None) -> Alert:
        now = utc_now()
        alert.status = "resolved"
        alert.last_seen_at = now
        alert.resolved_at = now
        if metadata is not None:
            alert.metadata_json = metadata
        db.session.commit()
        return alert

    def update_frame_ref(self, alert: Alert, frame_ref: str) -> Alert:
        alert.frame_ref = frame_ref
        alert.metadata_json = {**(alert.metadata_json or {}), "snapshot_available": True}
        db.session.commit()
        return alert

    def mark_false_positive(self, alert: Alert, *, reason: str | None = None) -> Alert:
        # Dicionário NOVO, nunca mutação in-place do que veio do banco: ver o
        # comentário de Alert.metadata_json em app/models.py.
        metadata = {**(alert.metadata_json or {}), "false_positive": True}
        if reason:
            metadata["false_positive_reason"] = reason
        metadata["false_positive_at"] = utc_now().isoformat()
        metadata["resolution_reason"] = metadata.get("resolution_reason") or "false_positive"
        if alert.status == "active":
            alert.status = "resolved"
            alert.resolved_at = utc_now()
        alert.last_seen_at = utc_now()
        alert.metadata_json = metadata
        db.session.commit()
        return alert

    def acknowledge(self, alert: Alert, *, note: str | None = None) -> Alert:
        """Marca que alguém tratou o alerta em campo. Não muda `status` — o
        alerta continua ativo enquanto a violação estiver acontecendo."""
        metadata = {**(alert.metadata_json or {}), "acknowledged": True, "acknowledged_at": utc_now().isoformat()}
        if note:
            metadata["acknowledged_note"] = note
        alert.metadata_json = metadata
        db.session.commit()
        return alert

    def list_recent(
        self,
        *,
        limit: int = 100,
        severity: str | None = None,
        status: str | None = None,
        false_positive: bool | None = None,
        camera_id: int | None = None,
        feature: str | None = None,
    ) -> list[Alert]:
        query = Alert.query
        if severity:
            query = query.filter(Alert.severity == severity)
        if feature:
            query = query.filter(Alert.feature == feature)
        if status:
            query = query.filter(Alert.status == status)
        if camera_id is not None:
            query = query.filter(Alert.camera_id == camera_id)
        if false_positive is not None:
            # Filtra no SQL, ANTES do LIMIT. Filtrar em Python depois do
            # .limit() fazia `?limit=100&false_positive=true` devolver só os
            # falsos positivos que por acaso estivessem entre os 100 mais
            # recentes — quase sempre menos itens do que o cliente pediu.
            flag = Alert.metadata_json["false_positive"].as_boolean()
            query = query.filter(flag.is_(True) if false_positive else db.or_(flag.is_(False), flag.is_(None)))
        return query.order_by(Alert.last_seen_at.desc()).limit(limit).all()

    def resolve_all_active(self, *, reason: str = "startup_reset", camera_id: int | None = None) -> int:
        """Resolve alertas ativos. `camera_id=None` significa TODAS as câmeras —
        use só em reset global. O start de uma câmera passa o próprio id: sem
        isso, iniciar a câmera 2 resolvia (e apagava da tela) os alertas vivos
        da câmera 1."""
        query = Alert.query.filter(Alert.status == "active")
        if camera_id is not None:
            query = query.filter(Alert.camera_id == camera_id)
        active_alerts = query.all()
        now = utc_now()
        for alert in active_alerts:
            alert.status = "resolved"
            alert.last_seen_at = now
            alert.resolved_at = now
            alert.metadata_json = {**(alert.metadata_json or {}), "resolution_reason": reason}
        db.session.commit()
        return len(active_alerts)

    def acknowledge_all_active(self, *, camera_id: int | None = None, note: str | None = None) -> int:
        """Marca como "avisei" todos os ativos que ainda nao foram tratados.

        So quem esta ativo e sem a marca: reconhecer de novo o que ja foi
        reconhecido sobrescreveria o horario original da auditoria.
        """
        query = Alert.query.filter(Alert.status == "active")
        if camera_id is not None:
            query = query.filter(Alert.camera_id == camera_id)
        agora = utc_now().isoformat()
        total = 0
        for alert in query.all():
            metadata = alert.metadata_json or {}
            if metadata.get("acknowledged"):
                continue
            novo = {**metadata, "acknowledged": True, "acknowledged_at": agora}
            if note:
                novo["acknowledged_note"] = note
            alert.metadata_json = novo
            total += 1
        db.session.commit()
        return total

    def delete_resolved(
        self, *, camera_id: int | None = None, older_than_days: int | None = None
    ) -> tuple[int, list[str]]:
        """Apaga SOMENTE alertas ja resolvidos. Ativo nunca e tocado.

        Devolve (quantos, nomes de evidencia que ficaram orfaos): quem chama
        remove os arquivos, e so os que nenhum alerta restante referencia.
        """
        from datetime import timedelta

        query = Alert.query.filter(Alert.status == "resolved")
        if camera_id is not None:
            query = query.filter(Alert.camera_id == camera_id)
        if older_than_days is not None:
            query = query.filter(Alert.last_seen_at < utc_now() - timedelta(days=int(older_than_days)))
        alvos = query.all()
        arquivos = [PurePosixPath(str(a.frame_ref)).name for a in alvos if a.frame_ref]
        for alert in alvos:
            db.session.delete(alert)
        db.session.commit()
        restantes = self.referenced_frame_filenames()
        return len(alvos), [nome for nome in arquivos if nome and nome not in restantes]

    def referenced_frame_filenames(self) -> set[str]:
        """Nomes de arquivo de evidencia que algum alerta ainda aponta.

        Usado pela limpeza de startup pra nao apagar snapshot que o historico
        ainda referencia (GET /alerts/<id>/evidence).
        """
        rows = db.session.query(Alert.frame_ref).filter(Alert.frame_ref.isnot(None)).all()
        return {PurePosixPath(str(row[0])).name for row in rows if row[0]}

    def list_active(self, *, limit: int = 100, camera_id: int | None = None) -> list[Alert]:
        return self.list_recent(limit=limit, status="active", camera_id=camera_id)
