import os
import httpx
from fastapi import HTTPException, Security, Header
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from typing import Optional

# API Key fija para el agente de Foundry. Configúrala como variable de entorno AGENT_API_KEY.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
# UPN fijo que se asignará a las llamadas del agente
AGENT_UPN = os.getenv("AGENT_UPN", "agente@foundry")

TENANT_ID = "c1f7b851-****-****-****-************"
CLIENT_ID = "4dd73579-****-****-****-************"
JWKS_URL = f"https://login.microsoftonline.com/{TENANT_ID}/discovery/v2.0/keys"
ISSUER = f"https://sts.windows.net/{TENANT_ID}/"

bearer_scheme = HTTPBearer()

_jwks_cache: dict | None = None


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            response = await client.get(JWKS_URL)
            response.raise_for_status()
            _jwks_cache = response.json()
    return _jwks_cache


async def get_current_upn(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
    x_api_key: Optional[str] = Header(default=None),
) -> str:
    # Autenticación por API Key para el agente de Foundry
    if x_api_key:
        if not AGENT_API_KEY:
            raise HTTPException(status_code=500, detail="AGENT_API_KEY no configurada en el servidor")
        if x_api_key != AGENT_API_KEY:
            raise HTTPException(status_code=401, detail="API Key inválida")
        return AGENT_UPN

    token = credentials.credentials
    try:
        jwks = await _get_jwks()
        header = jwt.get_unverified_header(token)
        key = next(
            (k for k in jwks["keys"] if k.get("kid") == header.get("kid")),
            None
        )
        if key is None:
            raise HTTPException(status_code=401, detail="Clave JWT no encontrada")

        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=f"api://{CLIENT_ID}",
            issuer=ISSUER,
            options={"verify_exp": True},
        )
    except JWTError as e:
        raise HTTPException(status_code=401, detail=f"Token inválido: {e}")

    upn = payload.get("upn") or payload.get("preferred_username") or payload.get("email")
    if not upn:
        raise HTTPException(status_code=401, detail="No se pudo extraer el UPN del token")
    return upn
