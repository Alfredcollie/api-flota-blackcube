# -*- coding: utf-8 -*-
"""
AUTH.PY - Manejo de claves y tokens para el control de acceso.

- Claves: PBKDF2-SHA256 (solo librería estándar, sin dependencias nuevas).
- Tokens: cadena aleatoria segura, guardada en la tabla 'tokens_acceso'.
"""
import hashlib
import hmac
import secrets

_ALGO = "sha256"
_ITERACIONES = 240000


def hash_password(password: str) -> str:
    """Encripta una clave y devuelve la cadena para guardar en la BD."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERACIONES)
    return "pbkdf2_%s$%d$%s$%s" % (_ALGO, _ITERACIONES, salt.hex(), dk.hex())


def verify_password(password: str, encoded: str) -> bool:
    """Verifica una clave contra la cadena guardada (comparación segura)."""
    try:
        algo, iteraciones, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_%s" % _ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        esperado = bytes.fromhex(hash_hex)
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, int(iteraciones))
        return hmac.compare_digest(dk, esperado)
    except Exception:
        return False


def generar_token() -> str:
    """Genera un token de acceso aleatorio y seguro."""
    return secrets.token_hex(32)
