"""Who is calling, and which sites they may touch.

Every /v1 route requires one of:

* ``Authorization: Bearer <ESG-lite JWT>``: browser calls. The token is the
  one ESG-lite issues at sign-in (HS256, payload ``{userId, role, siteId}``),
  verified with AUTH_JWT_SECRET (= ESG-lite's JWT_SECRET). The caller's role
  and sites are read from the shared ESG-lite tables, mirroring ESG-lite's
  ``accessibleSiteIds``: Superadmin sees every site, an Admin every site of
  their company, a User or Manager their own site(s).
* ``X-Service-Key: <AI_SERVICE_KEY>``: ESG-lite's server-to-server calls. That
  "service" principal is accepted only on /v1/column-config/* and
  /v1/sea-route (``allow_service``).

Routers declare the dependency in app/main.py; handlers that need the caller
take ``principal: Principal = Depends(require_user)`` (FastAPI resolves it
once per request).
"""
from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from psycopg2.extras import RealDictCursor

from app.core import database
from app.core.config import settings

logger = logging.getLogger(__name__)

SUPERADMIN = "Superadmin"
ADMIN = "Admin"


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "service"
    user_id: Optional[int] = None
    role: Optional[str] = None
    # None = every site (Superadmin); otherwise the sites the user may touch.
    site_ids: Optional[frozenset] = None

    @property
    def is_superadmin(self) -> bool:
        return self.kind == "user" and self.role == SUPERADMIN

    def can_access_site(self, site_id: Optional[int]) -> bool:
        if self.site_ids is None:
            return True
        return site_id is not None and site_id in self.site_ids

    def can_access_record(self, site_id: Optional[int], uploaded_by: Optional[int]) -> bool:
        """A stored record (invoice, document) with a site, or without one."""
        if self.is_superadmin:
            return True
        if site_id is None:
            return uploaded_by is not None and uploaded_by == self.user_id
        return self.can_access_site(site_id)


SERVICE = Principal(kind="service")

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="Sign in required",
    headers={"WWW-Authenticate": "Bearer"},
)


def _load_user_principal(user_id: int) -> Optional[Principal]:
    conn = database.get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute('SELECT user_id, role, site_id FROM "user" WHERE user_id = %s', (user_id,))
            user = cur.fetchone()
            if not user:
                return None
            role = user["role"]
            if role == SUPERADMIN:
                return Principal(kind="user", user_id=user_id, role=role, site_ids=None)

            cur.execute(
                "SELECT site_id FROM user_sites WHERE user_id = %s ORDER BY site_id", (user_id,)
            )
            own_sites = [r["site_id"] for r in cur.fetchall()]

            if role == ADMIN:
                # Company of the user's site, else of their first assigned site.
                anchor = user["site_id"] if user["site_id"] is not None else (own_sites[0] if own_sites else None)
                sites: set = set()
                if anchor is not None:
                    cur.execute(
                        """
                        SELECT s.site_id FROM site s
                        WHERE s.company_id IS NOT NULL
                          AND s.company_id = (SELECT company_id FROM site WHERE site_id = %s)
                        """,
                        (anchor,),
                    )
                    sites = {r["site_id"] for r in cur.fetchall()}
                return Principal(kind="user", user_id=user_id, role=role, site_ids=frozenset(sites))

            sites = set(own_sites)
            if user["site_id"] is not None:
                sites.add(user["site_id"])
            return Principal(kind="user", user_id=user_id, role=role, site_ids=frozenset(sites))
    finally:
        database.release_connection(conn)


def authenticate(request: Request) -> Principal:
    """Any signed-in caller: a user (Bearer token) or ESG-lite (service key)."""
    service_key = request.headers.get("x-service-key")
    if service_key is not None:
        expected = settings.AI_SERVICE_KEY or ""
        if expected and hmac.compare_digest(service_key.encode(), expected.encode()):
            return SERVICE
        raise _UNAUTHORIZED

    header = request.headers.get("authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _UNAUTHORIZED
    try:
        payload = jwt.decode(
            token.strip(),
            settings.AUTH_JWT_SECRET,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError:
        raise _UNAUTHORIZED

    user_id = payload.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise _UNAUTHORIZED

    try:
        principal = _load_user_principal(user_id)
    except Exception:
        logger.error("Could not load the caller's sites", exc_info=True)
        raise HTTPException(status_code=503, detail="Service temporarily unavailable")
    if principal is None:
        raise _UNAUTHORIZED
    return principal


def require_user(principal: Principal = Depends(authenticate)) -> Principal:
    """Signed-in users only; the service key is not enough."""
    if principal.kind != "user":
        raise HTTPException(status_code=403, detail="Not allowed")
    return principal


def require_superadmin(principal: Principal = Depends(require_user)) -> Principal:
    if not principal.is_superadmin:
        raise HTTPException(status_code=403, detail="Not allowed")
    return principal


def allow_service(principal: Principal = Depends(authenticate)) -> Principal:
    """Users or ESG-lite's service key (column-config, sea-route)."""
    return principal


# ---------------------------------------------------------------------------
# site_id carried by a request
# ---------------------------------------------------------------------------

def _coerce_site_id(value) -> Optional[int]:
    """Read a site_id the way the handlers will (int(), Pydantic int)."""
    if value is None or value == "":
        return None
    if isinstance(value, (bool, int)):
        return int(value)
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                number = None
            if number is not None and number.is_integer():
                return int(number)
    raise HTTPException(status_code=422, detail="site_id must be a whole number")


async def _request_site_ids(request: Request) -> list:
    values: list = list(request.query_params.getlist("site_id"))
    content_type = (request.headers.get("content-type") or "").lower()
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if content_type.startswith("multipart/form-data") or content_type.startswith(
            "application/x-www-form-urlencoded"
        ):
            form = await request.form()  # cached by Starlette; the handler reuses it
            values.extend(v for v in form.getlist("site_id") if isinstance(v, str))
        elif "json" in content_type:
            try:
                body = await request.json()
            except Exception:
                body = None  # the handler reports the bad body
            if isinstance(body, dict) and "site_id" in body:
                values.append(body["site_id"])
    return [s for s in (_coerce_site_id(v) for v in values) if s is not None]


async def enforce_site_scope(request: Request, principal: Principal = Depends(authenticate)) -> None:
    """403 when a request names a site_id (query, form or JSON body) the caller can't access."""
    if principal.kind != "user":
        return
    for site_id in await _request_site_ids(request):
        if not principal.can_access_site(site_id):
            raise HTTPException(status_code=403, detail="You do not have access to this site")

