from __future__ import annotations

import secrets
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from archivex.storage import ArchiveMedia, ArchiveRepository
from archivex.source import PostSource
from archivex.sync import ArchiveSyncService

PostType = Literal["original", "reply", "repost", "quote"]


class SessionLoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class ResolveAccountRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class AddAccountRequest(BaseModel):
    x_user_id: str = Field(pattern=r"^[0-9]+$", max_length=32)
    current_username: str = Field(pattern=r"^[A-Za-z0-9_]{1,15}$")
    display_name: str | None = Field(default=None, max_length=100)


class UpdateAccountRequest(BaseModel):
    archive_enabled: bool


def create_auth_router(auth_token: str, display_name: str, username: str,
                       avatar_url: str | None) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["authentication"])
    identity = {
        "display_name": display_name,
        "username": username,
        "avatar_url": avatar_url,
    }

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
    def get_session(request: Request) -> dict[str, Any]:
        authenticated = request.session.get("authenticated") is True
        return {"authenticated": authenticated, "user": identity if authenticated else None}

    @router.delete("/auth/session", status_code=status.HTTP_204_NO_CONTENT)
    def delete_session(request: Request) -> Response:
        request.session.clear()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


def create_api_router(repository: ArchiveRepository, auth_token: str, source: PostSource,
                      sync_service: ArchiveSyncService) -> APIRouter:
    router = APIRouter(prefix="/api", dependencies=[Depends(_require_token(auth_token))])

    @router.get("/accounts")
    def list_accounts() -> list[dict[str, Any]]:
        return repository.list_accounts()

    @router.post("/accounts/resolve")
    async def resolve_account(payload: ResolveAccountRequest) -> dict[str, Any]:
        username = _account_username(payload.query)
        try:
            account = await source.resolve_account(username)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="X account lookup failed") from exc
        if account is None:
            raise HTTPException(status_code=404, detail="X account not found")
        profile_image_url = account.profile_image_url
        if profile_image_url:
            profile_image_url = profile_image_url.replace("_normal.", "_400x400.")
        archived = repository.get_account(account.x_user_id)
        return {
            "x_user_id": account.x_user_id,
            "current_username": account.username,
            "display_name": account.display_name,
            "profile_image_url": profile_image_url,
            "description": account.description,
            "already_archived": archived is not None,
            "archive_enabled": archived.archive_enabled if archived else None,
        }

    @router.post("/accounts", status_code=status.HTTP_201_CREATED)
    def add_account(payload: AddAccountRequest, response: Response,
                    background_tasks: BackgroundTasks) -> dict[str, Any]:
        existing = repository.get_account(payload.x_user_id)
        account = repository.upsert_account(
            payload.x_user_id, payload.current_username, payload.display_name
        )
        background_tasks.add_task(sync_service.sync_account, account.x_user_id)
        if existing is not None:
            response.status_code = status.HTTP_200_OK
        return repository.get_account_details(account.x_user_id) or account.__dict__

    @router.get("/accounts/{x_user_id}")
    def get_account(x_user_id: str) -> dict[str, Any]:
        _validate_x_user_id(x_user_id)
        account = repository.get_account_details(x_user_id)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        return account

    @router.patch("/accounts/{x_user_id}")
    def update_account(x_user_id: str, payload: UpdateAccountRequest) -> dict[str, Any]:
        _validate_x_user_id(x_user_id)
        account = repository.set_account_enabled(x_user_id, payload.archive_enabled)
        if account is None:
            raise HTTPException(status_code=404, detail="account not found")
        return repository.get_account_details(x_user_id) or account.__dict__

    @router.post("/accounts/{x_user_id}/sync")
    async def sync_account(x_user_id: str) -> dict[str, Any]:
        _validate_x_user_id(x_user_id)
        if repository.get_account(x_user_id) is None:
            raise HTTPException(status_code=404, detail="account not found")
        return (await sync_service.sync_account(x_user_id)).__dict__

    @router.get("/accounts/{x_user_id}/username-history")
    def username_history(x_user_id: str) -> list[dict[str, Any]]:
        _validate_x_user_id(x_user_id)
        if repository.get_account(x_user_id) is None:
            raise HTTPException(status_code=404, detail="account not found")
        return repository.username_history(x_user_id)

    @router.get("/posts")
    def list_posts(
        account_x_user_id: str | None = None,
        q: str | None = Query(default=None, min_length=1, max_length=500),
        from_at: Annotated[datetime | None, Query(alias="from")] = None,
        to_at: Annotated[datetime | None, Query(alias="to")] = None,
        has_media: bool | None = None,
        post_type: PostType | None = None,
        exclude_post_type: PostType | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        if from_at and to_at and from_at > to_at:
            raise HTTPException(status_code=422, detail="from must be before or equal to to")
        posts = repository.list_posts(
            account_x_user_id=account_x_user_id, query=q, from_at=from_at, to_at=to_at,
            has_media=has_media, post_type=post_type, exclude_post_type=exclude_post_type,
            limit=limit, offset=offset,
        )
        return [_post_response(post, repository) for post in posts]

    @router.get("/posts/{tweet_id}")
    def get_post(tweet_id: str) -> dict[str, Any]:
        post = repository.get_post(tweet_id)
        if post is None:
            raise HTTPException(status_code=404, detail="post not found")
        return _post_response(post, repository)

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
        account_x_user_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [run.__dict__ for run in repository.list_sync_runs(
            account_x_user_id=account_x_user_id, limit=limit, offset=offset
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


def _post_response(post: Any, repository: ArchiveRepository) -> dict[str, Any]:
    response = post.__dict__.copy()
    response.update(repository.post_metrics(post.tweet_id))
    presentation = repository.post_presentation(post.tweet_id)
    if not presentation["display_text"] and not presentation["author_username"]:
        presentation["display_text"] = post.text
    response.update(presentation)
    response["media"] = [_media_response(media) for media in repository.post_media(post.tweet_id)]
    return response


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


def _account_username(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        parsed = urlparse(candidate)
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise HTTPException(status_code=422, detail="enter an X username or profile URL")
        parts = [part for part in parsed.path.split("/") if part]
        candidate = parts[0] if parts else ""
    candidate = candidate.lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", candidate):
        raise HTTPException(status_code=422, detail="invalid X username")
    return candidate


def _validate_x_user_id(value: str) -> None:
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise HTTPException(status_code=422, detail="invalid X user ID")
