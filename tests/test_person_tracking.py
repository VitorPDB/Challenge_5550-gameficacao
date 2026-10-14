"""Tracking de pessoa e associação geométrica EPI-pessoa."""
from __future__ import annotations

from app.services.compliance_service import ComplianceService
from app.services.feature_manager import FeatureManager
from app.services.risk_rules import RuleEngine
from app.vision import person_compliance_matcher
from app.vision.person_tracker import PersonTracker
from app.vision.schemas import BoundingBox, Detection

ALL_FEATURES = "ppe,helmet,vest,gloves,glasses,mask,safety_shoe,pose,falls,posture,risk_area"


def person(x1: int, y1: int = 100, width: int = 80, height: int = 300) -> Detection:
    return Detection(label="person", confidence=0.9, box=BoundingBox(x1, y1, x1 + width, y1 + height), category="person")


def helmet_on(p: Detection, confidence: float = 0.8) -> Detection:
    """Capacete na cabeça da pessoa: topo da caixa dela, totalmente contido."""
    box = BoundingBox(p.box.x1 + 20, p.box.y1 + 5, p.box.x1 + 60, p.box.y1 + 50)
    return Detection(label="helmet", confidence=confidence, box=box, category="ppe")


def manager():
    return FeatureManager.from_config({"DEFAULT_FEATURES": ALL_FEATURES})


def engine(supported=("helmet", "vest", "gloves")):
    return RuleEngine(
        manager(),
        cooldown_seconds=0,
        risk_polygon=[(0.7, 0.1), (1, 0.1), (1, 1), (0.7, 1)],
        supported_ppe_getter=lambda: set(supported),
    )


# ---------------------------------------------------------- PersonTracker ---
def test_mesma_pessoa_mantem_id_entre_frames():
    tracker = PersonTracker()
    ids = [tracker.update([person(100 + step * 10)])[0].track_id for step in range(5)]
    assert ids == [1, 1, 1, 1, 1]


def test_pessoa_nova_ganha_id_novo():
    tracker = PersonTracker()
    tracker.update([person(100)])
    tracked = tracker.update([person(105), person(400)])
    assert sorted(d.track_id for d in tracked) == [1, 2]


def test_ids_sobrevivem_ao_cruzamento_de_duas_pessoas():
    """O caso que quebrava: com ordenação espacial por x1, A e B trocam de
    identidade ao se cruzarem — e como o id entra na CHAVE do alerta, o alerta
    de uma "resolve" e o da outra "cria"."""
    tracker = PersonTracker()
    ids_de_a = []
    for step in range(9):
        a = person(100 + step * 20, y1=100)   # esquerda -> direita
        b = person(260 - step * 20, y1=140)   # direita -> esquerda, mais ao fundo
        tracked = tracker.update([a, b])
        # A é a detecção cujo y1 é 100.
        ids_de_a.append(next(d.track_id for d in tracked if d.box.y1 == 100))

    assert len(set(ids_de_a)) == 1, f"A trocou de identidade durante o cruzamento: {ids_de_a}"


def test_track_expira_depois_de_max_age():
    tracker = PersonTracker(max_age=2)
    assert tracker.update([person(100)])[0].track_id == 1
    for _ in range(3):
        tracker.update([])
    # Track antigo morreu: a pessoa que reaparece é tratada como nova.
    assert tracker.update([person(100)])[0].track_id == 2


def test_oclusao_curta_nao_perde_o_id():
    tracker = PersonTracker(max_age=15)
    assert tracker.update([person(100)])[0].track_id == 1
    for _ in range(5):
        tracker.update([])  # alguém passa na frente
    assert tracker.update([person(105)])[0].track_id == 1


def test_reset_zera_a_numeracao():
    tracker = PersonTracker()
    tracker.update([person(100)])
    tracker.reset()
    assert tracker.update([person(400)])[0].track_id == 1


# ----------------------------------------------- matching EPI <-> pessoa ----
def _build(detections):
    matcher = person_compliance_matcher.PersonComplianceMatcher()
    keys = person_compliance_matcher.PPE_KEYS
    return matcher.build(
        detections,
        supported_ppe={key: True for key in keys},
        enabled_ppe={key: True for key in keys},
    )


def test_capacete_conta_para_uma_pessoa_so():
    """Duas pessoas sobrepostas, UM capacete: ele é de quem o usa, não das duas.

    Antes bastava o centro do capacete cair na faixa "cabeça" da caixa da
    pessoa — sem exclusividade, o mesmo capacete satisfazia as duas.
    """
    a = person(100, y1=100)
    b = person(120, y1=140)   # sobreposta a A
    people = _build([a, b, helmet_on(b)])

    com_capacete = [p for p in people if p["ppe"]["helmet"]["status"] == "ok"]
    assert len(com_capacete) == 1


def test_capacete_vai_para_a_pessoa_de_melhor_encaixe():
    """Com sobreposição, ganha quem tem o capacete na altura mais plausível —
    não quem aparecer primeiro na lista."""
    a = person(100, y1=100)
    b = person(120, y1=140)
    people = _build([a, b, helmet_on(b)])

    dono = next(p for p in people if p["ppe"]["helmet"]["status"] == "ok")
    assert dono["box"]["y1"] == b.box.y1


def test_epi_fora_da_caixa_da_pessoa_nao_conta():
    p = person(100)
    longe = Detection(label="helmet", confidence=0.9, box=BoundingBox(600, 105, 640, 150), category="ppe")
    people = _build([p, longe])
    assert people[0]["ppe"]["helmet"]["status"] == "missing"


def test_calcado_na_cabeca_conta_como_uso_incorreto():
    """Faixa vertical importa: um `safety_shoe` detectado na altura da cabeça
    está SOBRE a pessoa, só na posição errada — "uso incorreto", não "ausente"
    (que é reservado a quando o item não aparece em lugar nenhum dela)."""
    p = person(100)
    na_cabeca = Detection(label="safety_shoe", confidence=0.9, box=BoundingBox(120, 105, 160, 150), category="ppe")
    people = _build([p, na_cabeca])
    assert people[0]["ppe"]["safety_shoe"]["status"] == "incorrect"
    assert people[0]["ppe"]["safety_shoe"]["detections"]  # o item incorreto continua listado


def test_uso_incorreto_nao_rebaixa_quem_ja_esta_ok():
    """Pessoa com capacete na cabeça (OK) e um segundo capacete solto perto do
    corpo: o veredito dela continua "ok" — a sobra incorreta não derruba quem
    já está em conformidade."""
    p = person(100)
    correto = helmet_on(p)
    solto = Detection(
        label="helmet", confidence=0.6,
        box=BoundingBox(p.box.x1 + 10, p.box.y1 + 200, p.box.x1 + 50, p.box.y1 + 240), category="ppe",
    )
    people = _build([p, correto, solto])
    assert people[0]["ppe"]["helmet"]["status"] == "ok"


def test_epi_incorreto_gera_alerta_proprio_sem_apagar_o_de_ausente_de_outro():
    """`incorrect_<epi>` é uma regra própria, distinta de `missing_<epi>` —
    a mensagem precisa dizer "uso incorreto", não "ausente"."""
    engine_local = engine(supported=("helmet", "vest", "gloves", "safety_shoe"))
    p = person(100)
    calcado_na_cabeca = Detection(
        label="safety_shoe", confidence=0.9,
        box=BoundingBox(p.box.x1 + 20, p.box.y1 + 5, p.box.x1 + 60, p.box.y1 + 50), category="ppe",
    )
    alerts = engine_local.evaluate([p, calcado_na_cabeca], pose=None, frame_shape=(450, 300, 3))
    incorreto = next(a for a in alerts if a.feature == "safety_shoe")
    assert incorreto.rule == "incorrect_safety_shoe"
    assert "incorreto" in incorreto.message.lower()


def test_duas_luvas_contam_para_a_mesma_pessoa():
    """Luvas são o único item com limite 2 por pessoa."""
    p = person(100, y1=100, width=80, height=300)
    luva_esq = Detection(label="gloves", confidence=0.8, box=BoundingBox(105, 280, 125, 310), category="ppe")
    luva_dir = Detection(label="gloves", confidence=0.8, box=BoundingBox(155, 280, 175, 310), category="ppe")
    people = _build([p, luva_esq, luva_dir])
    assert people[0]["ppe"]["gloves"]["status"] == "ok"
    assert len(people[0]["ppe"]["gloves"]["detections"]) == 2


def test_id_da_pessoa_usa_track_id_quando_existe():
    tracker = PersonTracker()
    tracked = tracker.update([person(100), person(400)])
    people = _build(tracked)
    assert {p["id"] for p in people} == {"person_1", "person_2"}
    assert [p["track_id"] for p in people] == [1, 2]


def test_alerta_segue_a_pessoa_certa_no_cruzamento():
    """Ponta a ponta: só A está sem capacete; a chave do alerta não pode pular
    pra outra pessoa quando elas se cruzam."""
    tracker = PersonTracker()
    rule_engine = engine(supported=("helmet",))
    chaves = []
    for step in range(9):
        a = person(100 + step * 20, y1=100)   # SEM capacete
        b = person(260 - step * 20, y1=140)   # COM capacete
        detections = tracker.update([a, b]) + [helmet_on(b)]
        alerts = [x for x in rule_engine.evaluate(detections, None, (480, 640, 3)) if x.rule == "missing_helmet"]
        assert len(alerts) == 1, f"frame {step}: esperava 1 alerta, veio {len(alerts)}"
        chaves.append(alerts[0].key)

    assert len(set(chaves)) == 1, f"o alerta trocou de dono durante o cruzamento: {chaves}"


# --------------------------------------------------- passada única (item 6) --
def test_matcher_roda_uma_vez_por_frame(monkeypatch):
    """RuleEngine + ComplianceService compartilham UMA avaliação.

    Antes o ComplianceService chamava `evaluate()` de novo, então o motor de
    regras rodava 2x e o matcher 3x por frame — a 12 FPS, por câmera.
    """
    chamadas = {"n": 0}
    original = person_compliance_matcher.PersonComplianceMatcher.build

    def contando(self, *args, **kwargs):
        chamadas["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(person_compliance_matcher.PersonComplianceMatcher, "build", contando)

    feature_manager = manager()
    rule_engine = engine()
    service = ComplianceService(feature_manager, rule_engine)
    detections = [person(10), person(150)]

    evaluation = rule_engine.analyze(detections, None, (480, 640, 3))
    service.build_state(
        detections=detections,
        pose=None,
        frame_shape=(480, 640, 3),
        model_diagnostics={"supported_ppe": {"helmet": True, "vest": True, "gloves": True}},
        active_alerts=[],
        evaluation=evaluation,
    )

    assert chamadas["n"] == 1


def test_compliance_service_ainda_funciona_sem_evaluation():
    """Chamada avulsa (sem o resultado pré-calculado) continua válida."""
    service = ComplianceService(manager(), engine())
    state = service.build_state(
        detections=[person(10)],
        pose=None,
        frame_shape=(480, 640, 3),
        model_diagnostics={"supported_ppe": {"helmet": True, "vest": True, "gloves": True}},
        active_alerts=[],
    )
    assert state["person_count"] == 1
    assert state["ppe"]["helmet"]["status"] == "missing"


def test_pessoa_que_muda_de_forma_sem_iou_mantem_o_id():
    """Sentou/inclinou: a caixa muda tanto que o IoU cai, mas o centro fica perto."""
    tracker = PersonTracker()
    a = tracker.update([person(100, y1=100, width=80, height=300)])[0].track_id
    sentada = Detection(label="person", confidence=0.9, box=BoundingBox(60, 260, 260, 380), category="person")
    assert tracker.update([sentada])[0].track_id == a


def test_pessoa_longe_ganha_id_novo_mesmo_com_fallback():
    tracker = PersonTracker()
    tracker.update([person(100)])
    assert tracker.update([person(900)])[0].track_id == 2
