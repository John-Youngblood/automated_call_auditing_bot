"""Signing in to the dashboard.

    POST   /api/session   password -> token
    DELETE /api/session   give the token back

Outside the guarded router, for the obvious reason: you cannot require a token
from the endpoint that issues one.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from starlette.requests import Request

from app.api.auth import SessionsDep, bearer_token
from app.schemas.calls import CamelModel

router = APIRouter(prefix="/api", tags=["session"])


class LoginRequest(CamelModel):
    password: str


class LoginResponse(CamelModel):
    token: str


@router.post("/session", summary="Exchange the dashboard password for a token")
async def log_in(payload: LoginRequest, sessions: SessionsDep) -> LoginResponse:
    token = sessions.log_in(payload.password)
    if token is None:
        # One message for a wrong password and for auth being switched off, so
        # the response does not say which.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "that password is not right")
    return LoginResponse(token=token)


@router.delete("/session", status_code=status.HTTP_204_NO_CONTENT)
async def log_out(request: Request, sessions: SessionsDep) -> None:
    sessions.log_out(bearer_token(request))
