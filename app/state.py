from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.security import LocalWebSecurity
from app.services.connection_manager import ConnectionManager
from app.services.local_file_service import LocalFileService
from app.services.profile_manager import ProfileManager
from app.services.progress_manager import ProgressManager
from app.services.transfer_manager import TransferManager
from app.storage import Storage


@dataclass
class AppState:
    settings: Settings
    storage: Storage
    security: LocalWebSecurity
    profiles: ProfileManager
    connections: ConnectionManager
    local: LocalFileService
    progress: ProgressManager
    transfers: TransferManager
