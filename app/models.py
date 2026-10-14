from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.mutable import MutableDict

from app.extensions import db
from app.llm import redigir_segredos


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Alert(db.Model):
    __tablename__ = "alerts"

    id = db.Column(db.Integer, primary_key=True)
    # Sem ForeignKey de propósito: o histórico de alertas precisa SOBREVIVER ao
    # DELETE da câmera (auditoria de segurança do trabalho não some porque
    # alguém descadastrou uma webcam). Indexado porque toda listagem/score
    # filtra por ele.
    camera_id = db.Column(db.Integer, nullable=True, index=True)
    rule = db.Column(db.String(80), nullable=False, index=True)
    severity = db.Column(db.String(30), nullable=False, index=True)
    message = db.Column(db.String(255), nullable=False)
    feature = db.Column(db.String(80), nullable=True, index=True)
    status = db.Column(db.String(30), nullable=False, default="active", index=True)
    frame_ref = db.Column(db.String(240), nullable=True)
    # MutableDict, não db.JSON puro: sem isso o SQLAlchemy não detecta mutação
    # in-place do dicionário (`meta = alert.metadata_json; meta["x"] = 1`) e a
    # escrita se perde silenciosamente no commit — era assim que a marcação de
    # falso positivo sumia do banco enquanto a resposta HTTP parecia correta.
    metadata_json = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=dict)
    occurrences = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    first_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    last_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)

    @property
    def key(self) -> str:
        metadata = self.metadata_json or {}
        subject = metadata.get("person_id") or metadata.get("subject") or "global"
        return f"{self.rule}:{self.feature or 'global'}:{subject}"

    def to_dict(self) -> dict:
        metadata = self.metadata_json or {}
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "key": self.key,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "feature": self.feature,
            "status": self.status,
            "frame_ref": self.frame_ref,
            "metadata": metadata,
            "false_positive": bool(metadata.get("false_positive")),
            "acknowledged": bool(metadata.get("acknowledged")),
            "occurrences": self.occurrences,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "first_seen_at": self.first_seen_at.isoformat() if self.first_seen_at else None,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class EventLog(db.Model):
    __tablename__ = "event_logs"

    id = db.Column(db.Integer, primary_key=True)
    # Mesmo raciocínio de Alert.camera_id (ver acima).
    camera_id = db.Column(db.Integer, nullable=True, index=True)
    event_type = db.Column(db.String(80), nullable=False, index=True)
    severity = db.Column(db.String(30), nullable=False, default="info", index=True)
    message = db.Column(db.String(255), nullable=False)
    subject = db.Column(db.String(120), nullable=True, index=True)
    # MutableDict pelo mesmo motivo de Alert.metadata_json (ver acima).
    metadata_json = db.Column(MutableDict.as_mutable(db.JSON), nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "camera_id": self.camera_id,
            "event_type": self.event_type,
            "severity": self.severity,
            "message": self.message,
            "subject": self.subject,
            "metadata": self.metadata_json or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# Features padrão de uma câmera nova — mesmas chaves que o FeatureManager já
# usa hoje (ver app/services/feature_manager.py), só que agora por câmera em
# vez de globais. Todas ligadas por padrão: uma câmera recém-criada deveria
# "ver tudo" até alguém restringir deliberadamente (oposto do Operador vs.
# Técnico: aqui quem decide é o Técnico, então o padrão é permissivo).
# Features padrão de uma câmera nova — precisa bater exatamente com as 8
# chaves de FeatureManager.AVAILABLE_FEATURES (app/services/feature_manager.py)
# e com o default real do sistema hoje (.env: DEFAULT_FEATURES=ppe,helmet,
# vest,gloves,pose,falls,posture,risk_area — todas ligadas). Diverge daqui
# e uma câmera nova nasce com comportamento diferente do que o .env sempre
# gerou, o que quebra a paridade que o seed do Passo 1 promete.
DEFAULT_CAMERA_FEATURES = {
    "ppe": True,
    "helmet": True,
    "vest": True,
    "gloves": True,
    "glasses": True,
    "mask": True,
    "safety_shoe": True,
    "ear_protection": True,
    "pose": True,
    "falls": True,
    "posture": True,
    "risk_area": True,
}


class Camera(db.Model):
    __tablename__ = "cameras"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(160), nullable=True)
    # "USB" (índice numérico em string), "RTSP" (URL) ou "Arquivo" (path) —
    # mesma distinção de fonte que MonitorService.parse_video_source já
    # entende; source_type é só pra UI (formulário/validação), o worker de
    # captura não olha pra ele, só pra `source`.
    source_type = db.Column(db.String(20), nullable=False, default="USB")
    source = db.Column(db.String(255), nullable=False)
    fps = db.Column(db.Integer, nullable=False, default=12)
    # Resolução de captura por câmera — antes era global (FRAME_WIDTH/
    # FRAME_HEIGHT no .env, uma só pra todas). Cada câmera tem hardware
    # diferente (ex.: webcam embutida 640x480 vs Logitech 1920x1080), então
    # forçar a mesma resolução pra todas ou reduz demais a de alta
    # qualidade, ou tenta um upscale impossível na de baixa qualidade.
    width = db.Column(db.Integer, nullable=False, default=960)
    height = db.Column(db.Integer, nullable=False, default=540)
    # Graus de rotação aplicados a cada frame ANTES de qualquer detecção —
    # existe porque câmera de planta às vezes é montada física de lado
    # (confirmado na Fresa 2: pessoas deitadas na imagem crua). Sem corrigir
    # aqui, YOLO e principalmente o MediaPipe Pose (que assume corpo
    # vertical) degradam muito, e a heurística de queda por orientação do
    # tronco passa a disparar em todo mundo. Só 0/90/180/270 — CSS não entra
    # (frame é array numpy, não elemento de tela).
    rotation = db.Column(db.Integer, nullable=False, default=0)
    # Polígono da área de risco DESTA câmera, lista de [x, y] normalizados
    # (0..1). None = usa RISK_AREA_POLYGON do .env. Cada câmera enxerga um
    # trecho diferente da planta, então a zona não pode ser uma só pra todas.
    risk_polygon = db.Column(db.JSON, nullable=True)
    # Modo portaria: EPIs obrigatórios. NULL = câmera fora do modo portaria.
    gate_required = db.Column(db.JSON, nullable=True)
    prevention_json = db.Column(db.JSON, nullable=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    features_json = db.Column(db.JSON, nullable=False, default=lambda: dict(DEFAULT_CAMERA_FEATURES))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "location": self.location,
            "source_type": self.source_type,
            "source": redigir_segredos(self.source or ""),
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "rotation": self.rotation,
            "risk_polygon": self.risk_polygon,
            "gate_required": self.gate_required,
            "prevention": self.prevention_json or {},
            "enabled": self.enabled,
            # Merge com os defaults, não substituição: uma câmera cadastrada
            # ANTES de uma feature nova existir tem o JSON sem aquela chave, e
            # devolvê-lo cru faria a UI ler `undefined` e mostrar a feature
            # como desligada — quando o comportamento real do worker é o
            # default (ligada). Foi o caso ao habilitar óculos/máscara/calçado.
            "features": {**DEFAULT_CAMERA_FEATURES, **(self.features_json or {})},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class PreventionMission(db.Model):
    __tablename__ = "prevention_missions"
    id = db.Column(db.Integer, primary_key=True)
    camera_id = db.Column(db.Integer, nullable=False, index=True)
    team = db.Column(db.String(120), nullable=False)
    category = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="open")
    evidence = db.Column(db.String(1000), nullable=True)
    created_by = db.Column(db.Integer, nullable=True)
    submitted_by = db.Column(db.Integer, nullable=True)
    verified_by = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utc_now, nullable=False)
    verified_at = db.Column(db.DateTime(timezone=True), nullable=True)

    def to_dict(self):
        return {"id": self.id, "camera_id": self.camera_id, "team": self.team, "category": self.category,
                "title": self.title, "status": self.status, "evidence": self.evidence,
                "created_by": self.created_by, "submitted_by": self.submitted_by, "verified_by": self.verified_by,
                "created_at": self.created_at.isoformat(),
                "verified_at": self.verified_at.isoformat() if self.verified_at else None,
                "points": 100 if self.status == "verified" else 0}


class ScenarioRun(db.Model):
    __tablename__ = "scenario_runs"
    id = db.Column(db.String(32), primary_key=True)
    camera_id = db.Column(db.Integer, nullable=False, index=True)
    owner_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="queued")
    result_json = db.Column(db.JSON, nullable=True)
    error = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    def to_dict(self):
        return {"id": self.id, "camera_id": self.camera_id, "status": self.status, "result": self.result_json,
                "error": self.error, "created_at": self.created_at.isoformat()}


# Papeis, do menor pro maior privilegio. A hierarquia e explicita aqui porque
# `require_role` compara NIVEL, nao igualdade: quem e supervisor passa em tudo
# que o tecnico passa. Sao os mesmos tres perfis que a UI ja tinha — a
# diferenca e que agora vem da sessao, nao de um seletor na topbar.
ROLE_OPERATOR = "operator"
ROLE_TECHNICAL = "technical"
ROLE_SUPERVISOR = "supervisor"
ROLE_LEVELS: dict[str, int] = {ROLE_OPERATOR: 1, ROLE_TECHNICAL: 2, ROLE_SUPERVISOR: 3}
VALID_ROLES = frozenset(ROLE_LEVELS)


class User(db.Model):
    """Pessoa real que opera o sistema.

    Sem cadastro publico: usuario e criado por quem ja tem acesso (CLI
    `flask users create` ou POST /api/users por um supervisor). Um sistema de
    seguranca do trabalho nao tem "criar conta".
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(180), nullable=False, unique=True, index=True)
    name = db.Column(db.String(120), nullable=False)
    # scrypt (padrao do werkzeug 3.x). A senha em si nunca e persistida nem
    # registrada em log.
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=ROLE_OPERATOR, index=True)
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    # Camera "do setor" do Operador. Substitui o seletor "simular setor" que
    # existia na topbar enquanto nao havia login. Sem ForeignKey pelo mesmo
    # motivo de Alert.camera_id: apagar uma camera nao pode apagar a pessoa.
    camera_id = db.Column(db.Integer, nullable=True, index=True)

    # Epoca da sessao. Toda sessao carrega a epoca vigente no momento do
    # login; `current_user` compara. Incrementar este numero invalida
    # IMEDIATAMENTE todo cookie ja emitido pra essa pessoa, inclusive um que
    # tenha sido copiado. E o que faz 'trocar a senha expulsa o invasor' e
    # 'desativar derruba a sessao' serem verdade, e nao so promessa.
    session_epoch = db.Column(db.Integer, nullable=False, default=1)

    # Protecao contra forca bruta — ver AuthService.
    failed_attempts = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)

    @property
    def level(self) -> int:
        return ROLE_LEVELS.get(self.role, 0)

    def has_role(self, minimo: str) -> bool:
        return self.level >= ROLE_LEVELS.get(minimo, 99)

    def to_dict(self) -> dict:
        """NUNCA inclui password_hash: este dicionario vai pro cliente."""
        return {
            "id": self.id,
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "camera_id": self.camera_id,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
