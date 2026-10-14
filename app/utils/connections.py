from __future__ import annotations

from threading import RLock

from flask import current_app

_versions: dict[int, int] = {}
_lock = RLock()


def connection_version(user_id: int) -> int:
    with _lock:
        return _versions.get(user_id, 0)


def user_room(user_id: int) -> str:
    return f"user:{user_id}"


def disconnect_user_sockets(user_id: int) -> None:
    """Revoga no servidor, sem depender de um evento enviado pelo cliente."""
    with _lock:
        _versions[user_id] = _versions.get(user_id, 0) + 1
    socketio = current_app.extensions.get("socketio")
    if socketio is None or socketio.server is None:
        return
    participants = list(socketio.server.manager.get_participants("/", user_room(user_id)))
    for sid, _ in participants:
        socketio.server.disconnect(sid, namespace="/")
