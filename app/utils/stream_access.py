from __future__ import annotations

from time import monotonic

from flask import current_app

from app.extensions import db
from app.models import ROLE_OPERATOR, User
from app.utils.auth import auth_enabled, current_user
from app.utils.connections import connection_version


def stream_access_check(camera_id: int | None):
    """Revoga no próximo frame via API; alterações externas são checadas a cada segundo."""
    if not auth_enabled():
        return lambda: True
    user = current_user()
    if user is None:
        return lambda: False
    app = current_app._get_current_object()
    user_id, epoch = user.id, user.session_epoch
    version = connection_version(user_id)
    next_check = 0.0

    def allowed() -> bool:
        nonlocal next_check
        if connection_version(user_id) != version:
            return False
        now = monotonic()
        if now < next_check:
            return True
        with app.app_context():
            latest = db.session.get(User, user_id, populate_existing=True)
            valid = bool(
                latest is not None
                and latest.active
                and latest.session_epoch == epoch
                and (latest.role != ROLE_OPERATOR or (camera_id is not None and latest.camera_id == camera_id))
            )
        next_check = now + 1.0 if valid else 0.0
        return valid

    return allowed
