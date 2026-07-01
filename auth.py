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
from typing import Tuple

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
