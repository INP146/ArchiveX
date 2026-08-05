from __future__ import annotations

import secrets
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from archivex.storage import ArchiveMedia, ArchiveRepository


class SessionLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


def create_auth_router(auth_token: str) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["authentication"])

    @router.post("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
    def create_session(payload: SessionLoginRequest, request: Request) -> Response:
        if not secrets.compare_digest(payload.token, auth_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid authentication token",
            )
        request.session["authenticated"] = True
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/auth/session")
    def get_session(request: Request) -> dict[str, bool]:
        return {"authenticated": request.session.get("authenticated") is True}

    @router.delete("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def create_api_router(repository: ArchiveRepository, auth_token: str) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(_require_token(auth_token))])

    @router.get("/accounts")
    def list_accounts() -> list[dict[str, Any]]:
        return repository.list_accounts()

    @router.get("/accounts/{account_id}")
    def get_account(account_id: int) -> dict[str, Any]:
        account = repository.get_account_by_id(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        return account

    @router.get("/posts")
    def list_posts(
        account_id: int | None = None,
        q: str | None = Query(default=None, min_length=1, max_length=500),
        from_at: Annotated[datetime | None, Query(alias="from")] = None,
        to_at: Annotated[datetime | None, Query(alias="to")] = None,
        has_media: bool | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        if from_at and to_at and from_at > to_at:
            raise HTTPException(status_code=422, detail="from must be before or equal to to")
        return [_post_response(post) for post in repository.list_posts(
            account_id=account_id, query=q, from_at=from_at, to_at=to_at,
            has_media=has_media, limit=limit, offset=offset,
        )]

    @router.get("/posts/{tweet_id}")
    def get_post(tweet_id: str) -> dict[str, Any]:
        post = repository.get_post(tweet_id)
        if post is None:
            raise HTTPException(status_code=404, detail="post not found")
        response = _post_response(post)
        response["media"] = [_media_response(media) for media in repository.post_media(tweet_id)]
        return response

    @router.get("/media/{media_id}")
    def get_media(media_id: str) -> FileResponse:
        media = repository.get_media(media_id)
        if media is None:
            raise HTTPException(status_code=404, detail="media not found")
        if media.download_status != "completed" or media.local_path is None:
            raise HTTPException(status_code=404, detail="media file is not available")
        path = _archive_path(repository.archive_data_dir, media.local_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="media file is not available")
        return FileResponse(path, filename=path.name)

    @router.get("/sync-runs")
    def list_sync_runs(
        account_id: int | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [run.__dict__ for run in repository.list_sync_runs(
            account_id=account_id, limit=limit, offset=offset
        )]

    return router


def _require_token(expected_token: str):
    security = HTTPBearer(auto_error=False)

    def require_token(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security),
    ) -> None:
        if request.session.get("authenticated") is True:
            return
        if credentials is None or not secrets.compare_digest(credentials.credentials, expected_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_token


def _post_response(post: Any) -> dict[str, Any]:
    return post.__dict__.copy()


def _media_response(media: ArchiveMedia) -> dict[str, Any]:
    return {
        "id": media.id,
        "media_type": media.media_type,
        "download_status": media.download_status,
        "sha256": media.sha256,
        "error": media.error,
        "url": f"/api/media/{media.id}" if media.download_status == "completed" and media.local_path else None,
    }


def _archive_path(archive_data_dir: Path, local_path: str) -> Path:
    base = archive_data_dir.resolve()
    path = (base / local_path).resolve()
    if base not in path.parents:
        raise HTTPException(status_code=404, detail="media file is not available")
    return path
