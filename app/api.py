from __future__ import annotations

import asyncio
from pathlib import Path

import asyncssh
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from app.models import (
    ConnectionCreate, ConnectionOut, ConnectionStatus, FileListResponse, PathRequest,
    ProfileCreate, ProfileOut, ProfileUpdate, RenameRequest, SSHHost, TransferCreate,
    TransferOut, RouteRequest,
)
from app.services.connection_manager import HostKeyConfirmationRequired, HostKeyMismatch
from app.services.transfer_manager import TransferConflictError
from app.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.state


router = APIRouter(prefix="/api")
events_router = APIRouter()


def error_response(status: int, message: str, code: str = "error"):
    return JSONResponse(status_code=status, content={"detail": message, "code": code})


@router.get("/config", response_model_exclude_none=True)
async def config(request: Request, state: AppState = Depends(get_state)):
    security = state.security
    security.check_host(request)
    return {
        "app_name": state.settings.app_name,
        "local_root": str(state.local.root),
        "local_drives_path": state.local.drives_path,
        "local_navigation_root": str(state.local.root) if state.settings.local_root else state.local.root.anchor,
        "token": security.token,
        "theme": state.settings.theme,
        "transfer_concurrency": state.settings.transfer_concurrency,
    }


@router.get("/ssh-hosts", response_model=list[SSHHost])
async def ssh_hosts(request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    return state.profiles.discover_ssh_hosts()


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    return await state.storage.list_profiles()


@router.post("/profiles", response_model=ProfileOut, status_code=201)
async def create_profile(payload: ProfileCreate, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    return await state.storage.create_profile(payload)


@router.patch("/profiles/{profile_id}", response_model=ProfileOut)
async def update_profile(profile_id: str, payload: ProfileUpdate, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.storage.update_profile(profile_id, payload)
    except KeyError:
        raise HTTPException(404, "Profile not found")
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(profile_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    await state.storage.delete_profile(profile_id)
    return Response(status_code=204)


@router.post("/connections", status_code=201)
async def create_connection(payload: ConnectionCreate, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    if payload.via_connection_id:
        try:
            state.connections.get(payload.via_connection_id)
        except KeyError:
            return error_response(409, "Jump connection is not active")
    try:
        if payload.profile_id:
            profile = await state.storage.get_profile(payload.profile_id)
            session = await state.connections.connect_profile(
                profile,
                password=payload.password,
                passphrase=payload.private_key_passphrase,
                confirm_host_key=payload.confirm_host_key,
                host_key_fingerprint=payload.host_key_fingerprint,
                via_connection_id=payload.via_connection_id,
            )
        elif payload.ssh_alias:
            session = await state.connections.connect_ssh_alias(
                payload.ssh_alias,
                password=payload.password,
                passphrase=payload.private_key_passphrase,
                confirm_host_key=payload.confirm_host_key,
                host_key_fingerprint=payload.host_key_fingerprint,
                via_connection_id=payload.via_connection_id,
            )
        else:
            raise HTTPException(422, "profile_id or ssh_alias is required")
    except HostKeyConfirmationRequired as exc:
        return JSONResponse(status_code=409, content={
            "detail": "Unknown SSH host key. Review the fingerprint and confirm to trust it.",
            "code": "host_key_confirmation_required",
            "host_key": exc.info.model_dump(),
        })
    except HostKeyMismatch as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc), "code": "host_key_mismatch"})
    except KeyError:
        raise HTTPException(404, "Profile not found")
    except (OSError, asyncssh.Error, ValueError, TimeoutError) as exc:
        detail = str(exc) or ("SSH/SFTP connection timed out" if isinstance(exc, TimeoutError) else type(exc).__name__)
        return JSONResponse(status_code=400, content={"detail": detail, "code": "connection_failed"})
    return session.out()


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    return state.connections.list()


@router.get("/connections/{connection_id}", response_model=ConnectionStatus)
async def connection_status(connection_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    try:
        return ConnectionStatus(connected=True, connection=state.connections.get(connection_id).out())
    except KeyError:
        return ConnectionStatus(connected=False)


@router.delete("/connections/{connection_id}", status_code=204)
async def disconnect(connection_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    await state.connections.disconnect(connection_id)
    return Response(status_code=204)


@router.post("/connection-route")
async def connection_route(payload: RouteRequest, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        _, route = await state.transfers.route(payload.source_connection_id, payload.destination_connection_id, payload.force)
        return route
    except KeyError:
        return error_response(409, "Remote connection is not active")


@router.get("/files/local", response_model=FileListResponse)
async def local_list(
    request: Request,
    path: str | None = None,
    show_hidden: bool = False,
    sort: str = "name:asc",
    state: AppState = Depends(get_state),
):
    state.security.check_http(request, modification=False)
    target = path or str(state.local.root)
    try:
        return await state.local.list_dir(target, show_hidden=show_hidden, sort=sort)
    except FileNotFoundError:
        return error_response(404, "Local directory not found")
    except PermissionError:
        return error_response(403, "Permission denied")
    except ValueError as exc:
        return error_response(400, str(exc))


@router.post("/files/local/mkdir", status_code=201)
async def local_mkdir(payload: PathRequest, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        path = Path(payload.path)
        result = await state.local.mkdir(str(path.parent), path.name)
        return {"path": result}
    except (FileExistsError, FileNotFoundError, PermissionError, ValueError) as exc:
        return error_response(400, str(exc))


@router.post("/files/local/rename")
async def local_rename(payload: RenameRequest, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        result = await state.local.rename(payload.path, payload.new_name)
        return {"path": result}
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        return error_response(400, str(exc))


@router.delete("/files/local")
async def local_delete(request: Request, path: str, recursive: bool = False, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        await state.local.delete(path, recursive=recursive)
        return Response(status_code=204)
    except FileNotFoundError:
        return error_response(404, "Local path not found")
    except (PermissionError, IsADirectoryError, OSError, ValueError) as exc:
        return error_response(400, str(exc))


@router.get("/files/remote", response_model=FileListResponse)
async def remote_list(
    request: Request,
    connection_id: str,
    path: str | None = None,
    show_hidden: bool = False,
    sort: str = "name:asc",
    state: AppState = Depends(get_state),
):
    state.security.check_http(request, modification=False)
    try:
        session = state.connections.get(connection_id)
        target = path or session.remote_root
        return await session.service.list_dir(target, show_hidden=show_hidden, sort=sort)
    except KeyError:
        return error_response(409, "Remote connection is not active")
    except asyncssh.SFTPError as exc:
        return error_response(400, f"SFTP error: {exc.reason or exc}")
    except (ValueError, OSError) as exc:
        return error_response(400, str(exc))


@router.post("/files/remote/mkdir", status_code=201)
async def remote_mkdir(payload: PathRequest, request: Request, connection_id: str, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        session = state.connections.get(connection_id)
        path = payload.path.rstrip("/")
        parent, name = path.rsplit("/", 1) if "/" in path else (".", path)
        result = await session.service.mkdir(parent, name)
        return {"path": result}
    except KeyError:
        return error_response(409, "Remote connection is not active")
    except asyncssh.SFTPError as exc:
        return error_response(400, f"SFTP error: {exc.reason or exc}")
    except (ValueError, OSError) as exc:
        return error_response(400, str(exc))


@router.post("/files/remote/rename")
async def remote_rename(payload: RenameRequest, request: Request, connection_id: str, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        session = state.connections.get(connection_id)
        result = await session.service.rename(payload.path, payload.new_name)
        return {"path": result}
    except KeyError:
        return error_response(409, "Remote connection is not active")
    except asyncssh.SFTPError as exc:
        return error_response(400, f"SFTP error: {exc.reason or exc}")
    except (ValueError, OSError) as exc:
        return error_response(400, str(exc))


@router.delete("/files/remote")
async def remote_delete(request: Request, connection_id: str, path: str, recursive: bool = False, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        session = state.connections.get(connection_id)
        await session.service.delete(path, recursive=recursive)
        return Response(status_code=204)
    except KeyError:
        return error_response(409, "Remote connection is not active")
    except asyncssh.SFTPError as exc:
        return error_response(400, f"SFTP error: {exc.reason or exc}")
    except (ValueError, OSError) as exc:
        return error_response(400, str(exc))


@router.post("/transfers", response_model=TransferOut, status_code=202)
async def create_transfer(payload: TransferCreate, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.transfers.create(payload)
    except KeyError:
        return error_response(409, "Remote connection is not active")
    except TransferConflictError as exc:
        return JSONResponse(status_code=409, content={
            "detail": f"Target already exists: {exc.path}",
            "code": "transfer_conflict",
            "path": exc.path,
        })
    except (FileNotFoundError, ValueError, OSError, asyncssh.SFTPError) as exc:
        return error_response(400, str(exc))


@router.get("/transfers", response_model=list[TransferOut])
async def list_transfers(request: Request, status: str | None = None, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    return await state.transfers.list(status)


@router.get("/transfers/{task_id}", response_model=TransferOut)
async def get_transfer(task_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=False)
    task = await state.storage.get_transfer(task_id)
    if not task:
        raise HTTPException(404, "Transfer not found")
    return task


@router.post("/transfers/{task_id}/pause", response_model=TransferOut)
async def pause_transfer(task_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.transfers.pause(task_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/transfers/{task_id}/resume", response_model=TransferOut)
async def resume_transfer(task_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.transfers.resume(task_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/transfers/{task_id}/cancel", response_model=TransferOut)
async def cancel_transfer(task_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.transfers.cancel(task_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/transfers/{task_id}/retry", response_model=TransferOut)
async def retry_transfer(task_id: str, request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    try:
        return await state.transfers.retry(task_id)
    except KeyError:
        raise HTTPException(404, "Transfer not found")


@router.post("/transfers/clear-finished", status_code=204)
async def clear_finished(request: Request, state: AppState = Depends(get_state)):
    state.security.check_http(request, modification=True)
    await state.transfers.clear_finished()
    return Response(status_code=204)


@events_router.websocket("/ws/events")
async def websocket_events(websocket: WebSocket) -> None:
    if not await websocket.app.state.state.security.check_websocket(websocket):
        return
    await websocket.accept()
    async def send_events():
        async for message in websocket.app.state.state.progress.subscribe():
            await websocket.send_text(message.model_dump_json())

    async def receive_disconnect():
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return

    tasks = [asyncio.create_task(send_events()), asyncio.create_task(receive_disconnect())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
