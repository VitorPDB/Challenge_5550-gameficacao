from __future__ import annotations

from app.repositories.alert_repository import AlertRepository
from app.services.alert_state_service import AlertStateService
from app.services.risk_rules import RuleAlert


class DummySocket:
    def __init__(self):
        self.events = []

    def emit(self, event, payload=None, *args, **kwargs):
        self.events.append((event, payload))


def missing_helmet():
    return RuleAlert(
        rule="missing_helmet",
        severity="critical",
        message="Sem capacete",
        feature="helmet",
        metadata={"present_labels": ["person"]},
    )


def test_alert_state_creates_and_resolves_after_confirmation_frames(app):
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(AlertRepository(), socket, create_after_frames=2, resolve_after_frames=2)

        first = service.process([missing_helmet()])
        assert first["active"] == []

        second = service.process([missing_helmet()])
        assert len(second["active"]) == 1
        assert second["active"][0]["status"] == "active"
        assert any(event == "alert_created" for event, _ in socket.events)

        still_active = service.process([])
        assert len(still_active["active"]) == 1

        resolved = service.process([])
        assert resolved["active"] == []
        assert len(resolved["resolved"]) == 1
        assert resolved["resolved"][0]["status"] == "resolved"
        assert any(event == "alert_resolved" for event, _ in socket.events)


def test_desligar_a_feature_encerra_o_alerta_na_hora(app):
    """Alerta de uma detecção desligada não espera a histerese: acaba no quadro
    seguinte, mesmo com `resolve_after_frames` alto e mesmo se o quadro só
    reaproveitou a análise anterior."""
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(AlertRepository(), socket, create_after_frames=1, resolve_after_frames=50)

        criado = service.process([missing_helmet()])
        assert len(criado["active"]) == 1

        # Ainda ligada: segue ativo.
        assert len(service.process([missing_helmet()], features_desligadas={"vest"})["active"]) == 1

        # Desligou o capacete: some agora, sem esperar 50 detecções limpas.
        depois = service.process([], deteccao_nova=False, features_desligadas={"helmet"})
        assert depois["active"] == []
        assert len(depois["resolved"]) == 1
        assert depois["resolved"][0]["status"] == "resolved"
        assert any(evento == "alert_resolved" for evento, _ in socket.events)


def test_frame_reaproveitado_nao_conta_como_confirmacao(app):
    """Histerese tem que contar DETECCAO, nao iteracao do loop.

    Com `DETECTION_EVERY_N_FRAMES=3`, dois de cada tres frames reaproveitam a
    mesma `FrameAnalysis` (caracterizado em tests/test_camera_worker_cache.py).
    Como o contador andava a cada chamada de `process`, uma unica inferencia
    real satisfazia sozinha o `create_after_frames=3` — a histerese
    documentada como "cria apos 3 frames ruins" exigia 1.

    Medido na fixture de 7 s: tracks vistos em UMA deteccao criaram 5 alertas
    cada, e a fixture toda gerou 76 alertas para 6 tracks.
    """
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(AlertRepository(), socket, create_after_frames=3, resolve_after_frames=2)

        # Uma deteccao real, vista tres vezes pelo loop: UMA confirmacao.
        assert service.process([missing_helmet()], deteccao_nova=True)["active"] == []
        assert service.process([missing_helmet()], deteccao_nova=False)["active"] == []
        assert service.process([missing_helmet()], deteccao_nova=False)["active"] == []
        assert not any(evento == "alert_created" for evento, _ in socket.events), (
            "uma deteccao vista tres vezes pelo loop nao pode criar alerta"
        )

        # Segunda deteccao real.
        service.process([missing_helmet()], deteccao_nova=True)
        assert not any(evento == "alert_created" for evento, _ in socket.events)

        # Terceira: agora sim.
        terceira = service.process([missing_helmet()], deteccao_nova=True)
        assert len(terceira["active"]) == 1
        assert any(evento == "alert_created" for evento, _ in socket.events)


def test_frame_reaproveitado_nao_conta_para_resolver(app):
    """O mesmo vale para o outro lado da histerese.

    Sem isto, um alerta resolveria depois de menos deteccoes limpas do que
    `resolve_after_frames` promete — e voltaria a ser criado no proximo frame
    ruim, que e a outra metade do churn medido.
    """
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(AlertRepository(), socket, create_after_frames=1, resolve_after_frames=3)

        assert len(service.process([missing_helmet()], deteccao_nova=True)["active"]) == 1

        assert service.process([], deteccao_nova=True)["resolved"] == []
        assert service.process([], deteccao_nova=False)["resolved"] == []
        assert service.process([], deteccao_nova=False)["resolved"] == []
        assert service.process([], deteccao_nova=True)["resolved"] == []

        resolvido = service.process([], deteccao_nova=True)
        assert len(resolvido["resolved"]) == 1, "tres deteccoes limpas deveriam resolver"


def test_alerta_ativo_nao_grava_a_cada_frame(app):
    """Renovar um alerta que continua ativo não pode gravar por frame.

    Cada gravação é um commit dentro do loop de captura (~9 ms). Com vários
    alertas e duas câmeras a 24 FPS isso saturava a CPU e travava o vídeo
    justamente quando havia infração. Aqui o intervalo é alto de propósito:
    depois de criado, nenhum frame seguinte pode gerar novo `alert_updated`.
    """
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(
            AlertRepository(), socket, create_after_frames=1, resolve_after_frames=99, intervalo_touch=3600.0
        )

        service.process([missing_helmet()])  # cria
        socket.events.clear()

        for _ in range(30):
            service.process([missing_helmet()])

        assert not [evento for evento, _ in socket.events if evento == "alert_updated"]
        # e o alerta segue ativo, com as ocorrências contadas em memória
        estado = next(iter(service._states.values()))
        assert estado.ocorrencias_pendentes == 30
        assert service.active_alerts()[0]["status"] == "active"


def test_ocorrencias_acumuladas_entram_na_gravacao(app):
    """Espaçar a gravação não pode PERDER contagem: o que ficou pendente
    precisa entrar de uma vez no próximo commit."""
    with app.app_context():
        socket = DummySocket()
        # intervalo_touch=0 grava sempre, então o acumulado vai junto na hora
        service = AlertStateService(
            AlertRepository(), socket, create_after_frames=1, resolve_after_frames=99, intervalo_touch=3600.0
        )
        service.process([missing_helmet()])
        base = service.active_alerts()[0]["occurrences"]

        for _ in range(9):
            service.process([missing_helmet()])  # nada é gravado ainda
        estado = next(iter(service._states.values()))
        assert estado.ocorrencias_pendentes == 9
        assert service.active_alerts()[0]["occurrences"] == base

        # força o vencimento do intervalo em vez de dormir no teste
        estado.ultimo_touch -= 7200.0
        service.process([missing_helmet()])

        # as 9 pendentes + esta entram de uma vez, nenhuma se perde
        assert service.active_alerts()[0]["occurrences"] == base + 10
        assert estado.ocorrencias_pendentes == 0


# ------------------------------------------------ soneca apos "resolver todos"
def _servico(socket=None):
    return AlertStateService(
        AlertRepository(), socket or DummySocket(), create_after_frames=2, resolve_after_frames=2
    )


def _ativos(service, frames=3):
    resultado = None
    for _ in range(frames):
        resultado = service.process([missing_helmet()])
    return resultado["active"]


def test_sem_soneca_o_alerta_volta_logo_depois_de_resolver_todos(app):
    """O comportamento que o usuario reportou: limpar e o mesmo alerta reaparece."""
    with app.app_context():
        service = _servico()
        assert len(_ativos(service)) == 1
        service.resolve_all(reason="manual_clear")
        assert len(_ativos(service)) == 1


def test_soneca_segura_o_alerta_enquanto_a_violacao_continua(app):
    with app.app_context():
        service = _servico()
        assert len(_ativos(service)) == 1
        service.resolve_all(reason="manual_clear", silenciar_s=60)
        assert _ativos(service, frames=10) == []


def test_soneca_acaba_com_o_tempo_e_o_risco_real_volta(app, monkeypatch):
    import app.services.alert_state_service as modulo

    with app.app_context():
        relogio = {"t": 1000.0}
        monkeypatch.setattr(modulo, "monotonic", lambda: relogio["t"])
        service = _servico()
        _ativos(service)
        service.resolve_all(reason="manual_clear", silenciar_s=60)
        assert _ativos(service) == []

        relogio["t"] += 61  # passou a soneca e ainda esta sem capacete
        assert len(_ativos(service)) == 1


def test_soneca_cai_quando_a_violacao_some_de_verdade(app, monkeypatch):
    """Pessoa colocou o capacete e depois tirou: violacao NOVA nao pode ficar muda."""
    import app.services.alert_state_service as modulo

    with app.app_context():
        relogio = {"t": 1000.0}
        monkeypatch.setattr(modulo, "monotonic", lambda: relogio["t"])
        service = _servico()
        _ativos(service)
        service.resolve_all(reason="manual_clear", silenciar_s=60)
        assert _ativos(service) == []

        # Sumiu por tempo suficiente pra ser "de verdade" (nao um piscar do rastreador).
        for _ in range(3):
            relogio["t"] += 4
            service.process([])
        assert len(_ativos(service)) == 1


def test_piscada_do_rastreador_nao_derruba_a_soneca(app, monkeypatch):
    """O rastreador perde a pessoa por um instante; isso nao pode reativar os alertas."""
    import app.services.alert_state_service as modulo

    with app.app_context():
        relogio = {"t": 1000.0}
        monkeypatch.setattr(modulo, "monotonic", lambda: relogio["t"])
        service = _servico()
        _ativos(service)
        service.resolve_all(reason="manual_clear", silenciar_s=60)

        for _ in range(6):  # 6 deteccoes sem ver a pessoa, mas so ~1 s de relogio
            relogio["t"] += 0.2
            service.process([])
        assert _ativos(service) == []


def test_soneca_e_so_da_chave_limpada(app):
    """Outra pessoa/regra continua alertando normalmente."""
    with app.app_context():
        service = _servico()
        _ativos(service)
        service.resolve_all(reason="manual_clear", silenciar_s=60)

        outra = RuleAlert(
            rule="missing_vest", severity="high", message="Sem colete", feature="vest", metadata={"person_id": "p2"}
        )
        for _ in range(3):
            resultado = service.process([missing_helmet(), outra])
        assert [a["rule"] for a in resultado["active"]] == ["missing_vest"]


def _com_caixa(person_id, x1, y1, x2, y2, rule="missing_helmet"):
    return RuleAlert(
        rule=rule,
        severity="critical",
        message="Sem capacete",
        feature="helmet",
        metadata={"person_id": person_id, "person_box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2}},
    )


def test_id_novo_da_mesma_pessoa_herda_a_soneca(app):
    """Rastreador trocou Pessoa 18 por Pessoa 26 na mesma posicao: e a mesma pessoa."""
    with app.app_context():
        service = _servico()
        for _ in range(3):
            service.process([_com_caixa("person_18", 100, 100, 300, 500)])
        service.resolve_all(reason="manual_clear", silenciar_s=60)

        for _ in range(4):
            resultado = service.process([_com_caixa("person_26", 110, 105, 305, 505)])
        assert resultado["active"] == []


def test_pessoa_diferente_em_outro_lugar_nao_e_silenciada(app):
    """Alguem novo entrando sem capacete tem que alertar mesmo durante a soneca."""
    with app.app_context():
        service = _servico()
        for _ in range(3):
            service.process([_com_caixa("person_18", 100, 100, 300, 500)])
        service.resolve_all(reason="manual_clear", silenciar_s=60)

        for _ in range(4):
            resultado = service.process([_com_caixa("person_30", 800, 100, 1000, 500)])
        assert [a["metadata"]["person_id"] for a in resultado["active"]] == ["person_30"]


def test_soneca_segue_a_pessoa_quando_ela_anda(app):
    with app.app_context():
        service = _servico()
        for _ in range(3):
            service.process([_com_caixa("person_18", 100, 100, 300, 500)])
        service.resolve_all(reason="manual_clear", silenciar_s=60)

        # anda aos poucos, mudando de id no caminho; cada passo sobrepoe o anterior
        for i, pid in enumerate(["person_19", "person_20", "person_21", "person_22"]):
            deslocamento = 80 * (i + 1)
            for _ in range(3):
                resultado = service.process([_com_caixa(pid, 100 + deslocamento, 100, 300 + deslocamento, 500)])
            assert resultado["active"] == [], pid


def test_identical_active_alerts_are_not_broadcast_for_every_frame(app):
    with app.app_context():
        socket = DummySocket()
        service = AlertStateService(AlertRepository(), socket, create_after_frames=1, intervalo_touch=3600)
        service.process([missing_helmet()])
        socket.events.clear()
        for _ in range(50):
            service.process([missing_helmet()], deteccao_nova=False)
        assert not [event for event, _ in socket.events if event == "active_alerts"]


def test_false_positive_stays_resolved_while_violation_continues(app):
    with app.app_context():
        repository = AlertRepository()
        service = AlertStateService(repository, DummySocket(), create_after_frames=1, intervalo_touch=0)
        payload = service.process([missing_helmet()])["created"][0]
        repository.mark_false_positive(repository.get(payload["id"]))
        for _ in range(3):
            result = service.process([missing_helmet()])
            assert result["active"] == []
            assert result["created"] == []
