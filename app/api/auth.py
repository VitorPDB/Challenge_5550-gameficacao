from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import ROLE_SUPERVISOR, VALID_ROLES, Camera, User
from app.services.auth_service import AuthError, AuthService, WeakPassword, normalize_email
from app.utils.auth import current_user, login_required, login_user, logout_user, require_role
from app.utils.connections import disconnect_user_sockets

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", ""))
    senha = str(payload.get("password", ""))
    if not email or not senha:
        return jsonify({"error": "Informe e-mail e senha."}), 400

    try:
        usuario = AuthService().authenticate(email, senha)
    except AuthError as exc:
        corpo = {"error": exc.message}
        if exc.retry_after is not None:
            corpo["retry_after"] = exc.retry_after
        return jsonify(corpo), exc.status

    login_user(usuario)
    return jsonify({"user": usuario.to_dict()})


@auth_bp.post("/api/auth/logout")
def logout():
    logout_user()
    return jsonify({"ok": True})


@auth_bp.get("/api/auth/me")
def me():
    """Quem esta logado. 200 com `user: null` quando ninguem esta.

    Nao devolve 401 de proposito: e a rota que o frontend chama no boot pra
    DECIDIR se mostra a tela de login. Um 401 aqui seria erro esperado no
    caminho normal, poluindo console e monitoramento.
    """
    usuario = current_user()
    return jsonify({"user": usuario.to_dict() if usuario else None})


@auth_bp.post("/api/auth/password")
@login_required(sempre=True)
def change_own_password():
    payload = request.get_json(silent=True) or {}
    atual = str(payload.get("current_password", ""))
    nova = str(payload.get("new_password", ""))
    usuario = current_user()

    # Exige a senha atual mesmo com sessao valida: protege contra um terminal
    # deixado aberto no chao de fabrica.
    try:
        AuthService().authenticate(usuario.email, atual, conta_como_tentativa=False)
    except AuthError:
        return jsonify({"error": "Senha atual incorreta."}), 403

    try:
        AuthService().set_password(usuario, nova)
    except WeakPassword as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True})


# ----------------------------------------------------- gestao de usuarios --
# Sem auto-cadastro: quem cria conta e o supervisor. Num sistema de seguranca
# do trabalho, "criar conta" nao e uma acao publica.
@auth_bp.get("/api/users")
@require_role(ROLE_SUPERVISOR, sempre=True)
def list_users():
    usuarios = User.query.order_by(User.name.asc()).all()
    return jsonify({"items": [u.to_dict() for u in usuarios], "count": len(usuarios)})


@auth_bp.post("/api/users")
@require_role(ROLE_SUPERVISOR, sempre=True)
def create_user():
    payload = request.get_json(silent=True) or {}
    # Regras de tela (o serviço fica permissivo para uso via CLI/testes): operador
    # exige uma câmera que EXISTA no sistema.
    camera_id = payload.get("camera_id")
    if payload.get("role") == "operator" and camera_id is None:
        return jsonify({"error": "Operador precisa de uma câmera do setor."}), 400
    if camera_id is not None and db.session.get(Camera, camera_id) is None:
        return jsonify({"error": "Câmera não cadastrada no sistema."}), 400
    try:
        usuario = AuthService().create_user(
            email=str(payload.get("email", "")),
            name=str(payload.get("name", "")),
            password=str(payload.get("password", "")),
            role=str(payload.get("role", "")),
            camera_id=payload.get("camera_id"),
        )
    except WeakPassword as exc:
        return jsonify({"error": str(exc)}), 400
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"user": usuario.to_dict()}), 201


@auth_bp.patch("/api/users/<int:user_id>")
@require_role(ROLE_SUPERVISOR, sempre=True)
def update_user(user_id: int):
    usuario = db.session.get(User, user_id)
    if usuario is None:
        return jsonify({"error": "usuário não encontrado"}), 404

    payload = request.get_json(silent=True) or {}
    atual = current_user()
    old_access = (usuario.role, usuario.camera_id, usuario.active)
    old_epoch = usuario.session_epoch

    if "role" in payload:
        papel = str(payload["role"])
        if papel not in VALID_ROLES:
            return jsonify({"error": f"Papel deve ser um de {sorted(VALID_ROLES)}."}), 400
        # Um supervisor nao pode se rebaixar: se for o ultimo, o sistema fica
        # sem ninguem capaz de gerir usuarios.
        if atual is not None and usuario.id == atual.id and papel != ROLE_SUPERVISOR:
            return jsonify({"error": "Você não pode alterar o próprio papel."}), 400
        usuario.role = papel

    if "name" in payload and str(payload["name"]).strip():
        usuario.name = str(payload["name"]).strip()[:120]
    if "email" in payload:
        email = normalize_email(str(payload["email"]))
        if not email or "@" not in email:
            return jsonify({"error": "E-mail inválido."}), 400
        existente = User.query.filter_by(email=email).first()
        if existente is not None and existente.id != usuario.id:
            return jsonify({"error": "Já existe usuário com este e-mail."}), 400
        usuario.email = email
    if "camera_id" in payload:
        usuario.camera_id = payload["camera_id"]
    if "active" in payload:
        ativo = bool(payload["active"])
        if atual is not None and usuario.id == atual.id and not ativo:
            return jsonify({"error": "Você não pode desativar a própria conta."}), 400
        if usuario.active and not ativo:
            AuthService.revoke_sessions(usuario, motivo="deactivated")
        usuario.active = ativo
    if payload.get("password"):
        # Trocar a PROPRIA senha exige a senha atual — senao o supervisor
        # furava, por esta rota, a trava que /api/auth/password impoe.
        if atual is not None and usuario.id == atual.id:
            return jsonify(
                {"error": "Para trocar a própria senha use POST /api/auth/password (exige a senha atual)."}
            ), 400
        try:
            AuthService().set_password(usuario, str(payload["password"]))
        except WeakPassword as exc:
            return jsonify({"error": str(exc)}), 400

    if old_access != (usuario.role, usuario.camera_id, usuario.active) and usuario.session_epoch == old_epoch:
        AuthService.revoke_sessions(usuario, motivo="access_changed")
    db.session.commit()
    if usuario.session_epoch != old_epoch:
        disconnect_user_sockets(usuario.id)
    return jsonify({"user": usuario.to_dict()})


@auth_bp.delete("/api/users/<int:user_id>")
@require_role(ROLE_SUPERVISOR, sempre=True)
def delete_user(user_id: int):
    usuario = db.session.get(User, user_id)
    if usuario is None:
        return jsonify({"error": "usuário não encontrado"}), 404
    atual = current_user()
    if atual is not None and usuario.id == atual.id:
        return jsonify({"error": "Você não pode apagar a própria conta."}), 400

    db.session.delete(usuario)
    db.session.commit()
    disconnect_user_sockets(user_id)
    logger.info("user_deleted", extra={"email": usuario.email})
    return jsonify({"deleted": True, "id": user_id})
