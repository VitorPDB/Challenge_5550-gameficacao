from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import VALID_ROLES, User, utc_now
from app.utils.connections import disconnect_user_sockets

logger = logging.getLogger(__name__)

# Forca bruta: apos N tentativas erradas seguidas, a conta trava por um tempo
# que dobra a cada nova rodada de falhas, ate o teto.
MAX_FAILED_ATTEMPTS = 5
BASE_LOCKOUT_MINUTES = 1
MAX_LOCKOUT_MINUTES = 30

# Politica minima de senha. Curta demais nao resiste a offline cracking mesmo
# com scrypt, e este e um sistema de seguranca do trabalho.
MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """Falha de autenticacao. `status` vira o codigo HTTP na rota."""

    def __init__(self, message: str, *, status: int = 401, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


class WeakPassword(ValueError):
    pass


def _aware(momento: datetime | None) -> datetime | None:
    """SQLite devolve datetime naive mesmo quando gravamos aware."""
    if momento is None:
        return None
    return momento if momento.tzinfo else momento.replace(tzinfo=timezone.utc)


def validate_password(senha: str) -> None:
    if len(senha or "") < MIN_PASSWORD_LENGTH:
        raise WeakPassword(f"A senha precisa de pelo menos {MIN_PASSWORD_LENGTH} caracteres.")


def hash_password(senha: str) -> str:
    validate_password(senha)
    return generate_password_hash(senha)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


class AuthService:
    """Autenticacao por e-mail e senha.

    A senha nunca e persistida nem registrada em log — so o hash scrypt gerado
    pelo werkzeug. `authenticate` e o unico ponto que compara senha.
    """

    def authenticate(self, email: str, senha: str, *, conta_como_tentativa: bool = True) -> User:
        """Confere e-mail e senha.

        `conta_como_tentativa=False` para quem JA esta autenticado e so esta
        reconfirmando a propria senha (troca de senha). Sem isso, errar a
        senha atual cinco vezes trancava o proprio login — auto-DoS por uma
        rota que ja exige sessao valida.
        """
        email = normalize_email(email)
        user = User.query.filter_by(email=email).first()

        agora = datetime.now(timezone.utc)
        travado_ate = _aware(user.locked_until) if user else None
        if user and travado_ate and travado_ate > agora and conta_como_tentativa:
            restante = int((travado_ate - agora).total_seconds())
            # Isto revela que a conta EXISTE. E uma troca consciente: sem a
            # mensagem, uma pessoa legitima travada fica sem entender por que
            # a senha certa nao entra. Enumeracao por lockout exige acertar o
            # e-mail e gastar 5 tentativas por conta, o que o proprio lockout
            # ja encarece.
            logger.warning("login_locked", extra={"email": email})
            raise AuthError(
                f"Muitas tentativas. Tente de novo em {max(1, restante // 60)} minuto(s).",
                status=429,
                retry_after=restante,
            )

        # Compara o hash mesmo com usuario inexistente ou inativo, pra que o
        # tempo de resposta nao denuncie quais e-mails existem.
        hash_alvo = user.password_hash if user else _HASH_FALSO
        senha_confere = check_password_hash(hash_alvo, senha or "")

        if user is None or not senha_confere or not user.active:
            if user is not None and senha_confere and not user.active:
                logger.warning("login_inactive_user", extra={"email": email})
            elif user is not None and conta_como_tentativa:
                self._register_failure(user)
            else:
                logger.info("login_unknown_email", extra={"email": email})
            raise AuthError("E-mail ou senha inválidos.")

        self._register_success(user)
        return user

    def _register_failure(self, user: User) -> None:
        user.failed_attempts = int(user.failed_attempts or 0) + 1
        if user.failed_attempts >= MAX_FAILED_ATTEMPTS:
            rodadas = user.failed_attempts - MAX_FAILED_ATTEMPTS
            minutos = min(BASE_LOCKOUT_MINUTES * (2**rodadas), MAX_LOCKOUT_MINUTES)
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=minutos)
            logger.warning("login_lockout", extra={"email": user.email, "minutes": minutos})
        db.session.commit()

    def _register_success(self, user: User) -> None:
        user.failed_attempts = 0
        user.locked_until = None
        user.last_login_at = utc_now()
        db.session.commit()
        logger.info("login_success", extra={"email": user.email, "role": user.role})

    # ------------------------------------------------------------ cadastro --
    def create_user(
        self,
        *,
        email: str,
        name: str,
        password: str,
        role: str,
        camera_id: int | None = None,
    ) -> User:
        email = normalize_email(email)
        if not email or "@" not in email:
            raise ValueError("E-mail inválido.")
        if not (name or "").strip():
            raise ValueError("Nome é obrigatório.")
        if role not in VALID_ROLES:
            raise ValueError(f"Papel deve ser um de {sorted(VALID_ROLES)}.")
        if User.query.filter_by(email=email).first() is not None:
            raise ValueError("Já existe usuário com este e-mail.")

        user = User(
            email=email,
            name=name.strip()[:120],
            password_hash=hash_password(password),
            role=role,
            camera_id=camera_id,
        )
        db.session.add(user)
        db.session.commit()
        logger.info("user_created", extra={"email": email, "role": role})
        return user

    def set_password(self, user: User, senha: str) -> User:
        user.password_hash = hash_password(senha)
        user.failed_attempts = 0
        user.locked_until = None
        # Trocar a senha DERRUBA todas as sessoes existentes — e a resposta
        # padrao a uma conta comprometida, e sem isto o invasor com o cookie
        # copiado continuaria dentro.
        self.revoke_sessions(user, motivo="password_changed")
        db.session.commit()
        logger.info("password_changed", extra={"email": user.email})
        disconnect_user_sockets(user.id)
        return user

    @staticmethod
    def revoke_sessions(user: User, *, motivo: str) -> None:
        """Invalida todo cookie ja emitido pra esta pessoa.

        Nao faz commit: quem chama decide o momento (as vezes ha mais coisa na
        mesma transacao).
        """
        user.session_epoch = int(user.session_epoch or 1) + 1
        logger.info("sessions_revoked", extra={"email": user.email, "reason": motivo})


# Hash descartavel usado quando o e-mail nao existe: manter o custo de
# verificacao constante evita que o tempo de resposta vire um oraculo de quais
# contas existem. Gerado uma vez, na importacao.
_HASH_FALSO = generate_password_hash("nao-e-uma-senha-real-apenas-custo-constante")
