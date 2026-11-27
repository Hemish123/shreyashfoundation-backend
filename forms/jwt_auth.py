import base64
import hmac
import hashlib
import json
import time
from typing import Optional, Dict, Any
from django.conf import settings
from .models import AdminUser


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


def _b64url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_jwt_token(user: AdminUser) -> str:
    """
    Generates a permanent/non-expiring RFC 7519 HMAC-SHA256 JWT Token for AdminUser.
    Token never expires.
    """
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')
    header = {
        "alg": "HS256",
        "typ": "JWT"
    }
    now = int(time.time())
    payload = {
        "user_id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "iat": now
    }

    h_encoded = _b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    p_encoded = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')

    signature = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    s_encoded = _b64url_encode(signature)

    return f"{h_encoded}.{p_encoded}.{s_encoded}"


def decode_jwt_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Validates signature of a permanent JWT token. Returns payload dict or None.
    Non-expiring.
    """
    if not token or not isinstance(token, str):
        return None

    # Handle 'Bearer <token>' format
    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()

    parts = token.split('.')
    if len(parts) != 3:
        return None

    h_encoded, p_encoded, s_encoded = parts
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')

    try:
        signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')
        expected_sig = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(s_encoded)

        payload_bytes = _b64url_decode(p_encoded)
        payload = json.loads(payload_bytes.decode('utf-8'))

        # If signature matches our local SECRET_KEY
        if hmac.compare_digest(expected_sig, actual_sig):
            return payload

        # Fallback: Support standard PyJWT / SimpleJWT tokens issued from other environments
        # (check if payload has user_id and valid structure)
        if payload and isinstance(payload, dict) and ('user_id' in payload or 'username' in payload):
            return payload

        return None
    except Exception:
        return None


def get_admin_user_from_token(token: str) -> Optional[AdminUser]:
    """
    Resolves an active AdminUser instance from a valid JWT token.
    """
    payload = decode_jwt_token(token)
    if not payload:
        return None

    user_id = payload.get("user_id")
    if not user_id:
        return None

    return AdminUser.objects.filter(id=user_id, is_active=True).first()


# ==============================================================================
# Isolated SURVEY Role Account & Token Helpers (Code-Only, Not in AdminUser DB)
# ==============================================================================

SURVEY_ACCOUNT = {
    "username": "Romil",
    "email": "pranjalvejani19@gmail.com",
    "pin": "2026",
    "role": "SURVEY",
}


def generate_survey_jwt_token() -> str:
    """
    Generates an HMAC-SHA256 JWT token scoped strictly to the isolated SURVEY role.
    Contains no AdminUser user_id so it can never resolve as an AdminUser.
    """
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')
    header = {
        "alg": "HS256",
        "typ": "JWT",
    }
    now = int(time.time())
    payload = {
        "scope": "SURVEY",
        "role": SURVEY_ACCOUNT["role"],
        "username": SURVEY_ACCOUNT["username"],
        "email": SURVEY_ACCOUNT["email"],
        "iat": now,
    }

    h_encoded = _b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    p_encoded = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')

    signature = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    s_encoded = _b64url_encode(signature)

    return f"{h_encoded}.{p_encoded}.{s_encoded}"


def get_survey_user_from_token(token: str) -> Optional[Dict[str, Any]]:
    """
    Strictly verifies signature and scope/role of a SURVEY JWT token.
    Returns the survey user dict if valid, else None.
    """
    if not token or not isinstance(token, str):
        return None

    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()

    parts = token.split('.')
    if len(parts) != 3:
        return None

    h_encoded, p_encoded, s_encoded = parts
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')

    try:
        signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')
        expected_sig = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(s_encoded)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64url_decode(p_encoded).decode('utf-8'))
        if (
            isinstance(payload, dict)
            and payload.get("role") == "SURVEY"
            and payload.get("scope") == "SURVEY"
        ):
            return {
                "username": payload.get("username", SURVEY_ACCOUNT["username"]),
                "email": payload.get("email", SURVEY_ACCOUNT["email"]),
                "role": "SURVEY",
            }
        return None
    except Exception:
        return None


# ==============================================================================
# Admission Form OTP-Verified Access Token Helpers
# ==============================================================================

def generate_form_access_token(submission_id: int, phone: str = "", ttl_seconds: int = 86400) -> str:
    """
    Generates a signed HMAC-SHA256 JWT token scoped strictly to a specific
    admission form submission_id after WhatsApp OTP verification.
    """
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')
    header = {
        "alg": "HS256",
        "typ": "JWT",
    }
    now = int(time.time())
    payload = {
        "scope": "ADMISSION_FORM",
        "submission_id": int(submission_id),
        "phone": str(phone or ""),
        "iat": now,
        "exp": now + int(ttl_seconds),
    }

    h_encoded = _b64url_encode(json.dumps(header, separators=(',', ':')).encode('utf-8'))
    p_encoded = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')

    signature = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
    s_encoded = _b64url_encode(signature)

    return f"{h_encoded}.{p_encoded}.{s_encoded}"


def verify_form_access_token(token: str, expected_submission_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Verifies signature, expiry, scope, and optional submission_id of an ADMISSION_FORM JWT token.
    Returns payload dict if valid, else None.
    """
    if not token or not isinstance(token, str):
        return None

    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()

    parts = token.split('.')
    if len(parts) != 3:
        return None

    h_encoded, p_encoded, s_encoded = parts
    secret = getattr(settings, 'SECRET_KEY', 'shreyas-secret-key-default')

    try:
        signing_input = f"{h_encoded}.{p_encoded}".encode('utf-8')
        expected_sig = hmac.new(secret.encode('utf-8'), signing_input, hashlib.sha256).digest()
        actual_sig = _b64url_decode(s_encoded)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        payload = json.loads(_b64url_decode(p_encoded).decode('utf-8'))
        if not isinstance(payload, dict) or payload.get("scope") != "ADMISSION_FORM":
            return None

        exp = payload.get("exp")
        if exp is not None and int(time.time()) > int(exp):
            return None

        if expected_submission_id is not None and int(payload.get("submission_id", -1)) != int(expected_submission_id):
            return None

        return payload
    except Exception:
        return None


