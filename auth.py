"""
Простая session-based авторизация для веб-версии.

Модель безопасности (MVP для внутреннего инструмента):
  * единственный администратор: ADMIN_EMAIL / ADMIN_PASSWORD из env;
  * пароль можно сменить в UI — тогда хранится pbkdf2-хэш (в БД/настройках),
    а не открытый текст;
  * до входа приложение не показывает ни данные, ни настройки.

Секреты берутся только из переменных окружения / настроек, не хардкодятся.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional, Tuple

_ITERATIONS = 120_000


def hash_password(password: str, salt: bytes = b"") -> str:
    """Вернуть строку 'pbkdf2$iter$salt_hex$hash_hex'."""
    if not salt:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2${}${}${}".format(_ITERATIONS, salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против строки, полученной hash_password()."""
    try:
        scheme, iters, salt_hex, hash_hex = stored.split("$", 3)
        if scheme != "pbkdf2":
            return False
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt, int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def is_configured(settings) -> bool:
    """True, если задан пароль администратора (через env или через UI-хэш)."""
    return bool(
        (getattr(settings, "admin_password_hash", "") or "").strip()
        or (getattr(settings, "admin_password", "") or "").strip()
    )


def check_credentials(email: str, password: str, settings) -> bool:
    """Проверить логин/пароль администратора."""
    admin_email = (getattr(settings, "admin_email", "") or "").strip().lower()
    if not email or email.strip().lower() != admin_email:
        return False

    stored_hash = (getattr(settings, "admin_password_hash", "") or "").strip()
    if stored_hash:
        return verify_password(password, stored_hash)

    env_password = (getattr(settings, "admin_password", "") or "")
    if not env_password:
        return False
    return hmac.compare_digest((password or ""), env_password)


def set_admin_password(new_password: str, settings) -> Tuple[bool, str]:
    """
    Сменить пароль администратора: сохранить его pbkdf2-хэш в настройках/БД.
    Возвращает (успех, сообщение).
    """
    if not new_password or len(new_password) < 8:
        return (False, "Пароль должен быть не короче 8 символов.")
    try:
        from config.settings import save_settings

        settings.admin_password_hash = hash_password(new_password)
        # Открытый bootstrap-пароль больше не нужен.
        settings.admin_password = ""
        save_settings(settings)
        return (True, "Пароль администратора обновлён.")
    except Exception as exc:  # pragma: no cover
        return (False, "Не удалось сохранить пароль: {}".format(exc))


# ---------------------------------------------------------------------------
# Сессионные токены (переживают перезагрузку страницы)
# ---------------------------------------------------------------------------
# Токен кладётся в query-параметр ?auth=... после входа; при перезагрузке
# страницы Streamlit-сессия теряется, но токен из URL восстанавливает вход.
# Формат: base64url(email).expiry_ts.hexsig, где hexsig = HMAC-SHA256 по
# строке "email|expiry_ts" с ключом settings.session_secret.

_SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 дней


# Значения секрета, при которых токен-авторизация ОТКЛЮЧАЕТСЯ (fail-closed):
# при дефолтном/пустом/коротком SESSION_SECRET ключ подписи предсказуем, и токен
# из URL можно подделать (обход входа). Тогда авто-вход по ?auth не работает —
# остаётся только вход по паролю.
_INSECURE_SECRETS = {"", "change-me"}


def secret_is_secure(settings) -> bool:
    """True, если SESSION_SECRET задан и достаточно надёжен для токен-авторизации."""
    try:
        s = (getattr(settings, "session_secret", "") or "").strip()
        return s not in _INSECURE_SECRETS and len(s) >= 16
    except Exception:
        return False


def _session_secret(settings) -> bytes:
    """Ключ подписи токена из настроек (никогда не бросает)."""
    try:
        return ((getattr(settings, "session_secret", "") or "change-me").encode("utf-8"))
    except Exception:
        return b"change-me"


def issue_session_token(email: str, settings) -> str:
    """
    Выпустить подписанный токен сессии для email (срок — 7 дней).

    Если SESSION_SECRET не задан/дефолтный/короткий — возвращаем пустую строку
    (токен-авторизация отключена, вход только по паролю). Иначе — не бросает.
    """
    try:
        import base64
        import time

        if not secret_is_secure(settings):
            return ""
        email_norm = (email or "").strip()
        if not email_norm:
            return ""
        expiry_ts = int(time.time()) + _SESSION_TTL_SECONDS
        payload = "{}|{}".format(email_norm, expiry_ts).encode("utf-8")
        sig = hmac.new(_session_secret(settings), payload, hashlib.sha256).hexdigest()
        email_b64 = (
            base64.urlsafe_b64encode(email_norm.encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        return "{}.{}.{}".format(email_b64, expiry_ts, sig)
    except Exception:
        return ""


def verify_session_token(token: str, settings) -> Optional[str]:
    """
    Проверить токен сессии. Возвращает email при валидном и непросроченном
    токене, иначе None. Сравнение подписи — константное по времени. Не бросает.
    """
    try:
        import base64
        import time

        # Fail-closed: без надёжного секрета токен подделываем — не принимаем.
        if not secret_is_secure(settings):
            return None
        parts = (token or "").strip().split(".")
        if len(parts) != 3:
            return None
        email_b64, expiry_str, sig = parts
        padding = "=" * (-len(email_b64) % 4)
        email = base64.urlsafe_b64decode(
            (email_b64 + padding).encode("ascii")
        ).decode("utf-8")
        expiry_ts = int(expiry_str)
        if expiry_ts < int(time.time()):
            return None
        payload = "{}|{}".format(email, expiry_ts).encode("utf-8")
        expected = hmac.new(_session_secret(settings), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        return email
    except Exception:
        return None
