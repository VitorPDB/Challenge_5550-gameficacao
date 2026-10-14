"""Modo portaria: só aprova com TODOS os EPIs exigidos, e na dúvida nega."""
from __future__ import annotations

import pytest
from app.services.gate_service import GateState, avaliar_quadro, normalizar_exigidos


def pessoa(**status):
    return {"ppe": {chave: {"status": valor} for chave, valor in status.items()}}


def test_aprova_quando_todos_exigidos_estao_ok():
    r = avaliar_quadro({"people": [pessoa(helmet="ok", vest="ok", gloves="missing")]}, ["helmet", "vest"])
    assert r["verdict"] == "approved"


def test_nega_e_lista_o_que_falta():
    r = avaliar_quadro({"people": [pessoa(helmet="ok", vest="missing")]}, ["helmet", "vest"])
    assert r == {"verdict": "denied", "missing": ["vest"], "people": 1}


@pytest.mark.parametrize("status", ["unsupported", "disabled", "unknown"])
def test_na_duvida_nega(status):
    assert avaliar_quadro({"people": [pessoa(helmet=status)]}, ["helmet"])["verdict"] == "denied"


def test_com_duas_pessoas_basta_uma_sem_epi_para_negar():
    r = avaliar_quadro({"people": [pessoa(helmet="ok"), pessoa(helmet="missing")]}, ["helmet"])
    assert r["verdict"] == "denied"


def test_sem_ninguem_aguarda():
    assert avaliar_quadro({"people": []}, ["helmet"])["verdict"] == "waiting"
    assert avaliar_quadro(None, ["helmet"])["verdict"] == "waiting"


def test_veredito_so_muda_depois_de_confirmar():
    estado = GateState()
    ok = {"verdict": "approved", "missing": [], "people": 1}
    for _ in range(GateState.CONFIRMACAO["approved"] - 1):
        assert estado.atualizar(ok)["verdict"] == "waiting"
    assert estado.atualizar(ok)["verdict"] == "approved"


def test_um_quadro_ruim_no_meio_zera_a_confirmacao_de_aprovacao():
    estado = GateState()
    ok = {"verdict": "approved", "missing": [], "people": 1}
    ruim = {"verdict": "denied", "missing": ["helmet"], "people": 1}
    for _ in range(5):
        estado.atualizar(ok)
    estado.atualizar(ruim)
    for _ in range(GateState.CONFIRMACAO["approved"] - 1):
        assert estado.atualizar(ok)["verdict"] != "approved"


def test_negar_e_mais_rapido_que_aprovar():
    assert GateState.CONFIRMACAO["denied"] < GateState.CONFIRMACAO["approved"]


def test_normalizar_exigidos():
    assert normalizar_exigidos(None) is None
    assert normalizar_exigidos(["helmet", "helmet", "vest"]) == ["helmet", "vest"]
    with pytest.raises(ValueError):
        normalizar_exigidos([])
    with pytest.raises(ValueError):
        normalizar_exigidos(["chapeu"])


def test_api_liga_le_e_desliga(real_monitor_client):
    r = real_monitor_client.post("/api/cameras", json={"name": "Portaria", "source_type": "USB", "source": "0"})
    cam = r.get_json()["id"]

    assert real_monitor_client.get(f"/api/cameras/{cam}/gate").get_json()["verdict"] == "off"

    r = real_monitor_client.put(f"/api/cameras/{cam}/gate", json={"required": ["helmet", "vest"]})
    assert r.status_code == 200 and r.get_json()["required"] == ["helmet", "vest"]
    corpo = real_monitor_client.get(f"/api/cameras/{cam}/gate").get_json()
    assert corpo["enabled"] is True and corpo["required"] == ["helmet", "vest"]
    assert real_monitor_client.get(f"/api/cameras/{cam}").get_json()["gate_required"] == ["helmet", "vest"]

    assert real_monitor_client.put(f"/api/cameras/{cam}/gate", json={"required": ["chapeu"]}).status_code == 400
    assert real_monitor_client.put(f"/api/cameras/{cam}/gate", json={"required": None}).get_json()["enabled"] is False
