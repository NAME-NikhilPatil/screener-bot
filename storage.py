from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

class LocalJsonStorage:
    def load_json(self, name: str, default: Any) -> Any:
        path = Path(name)
        if not path.exists():
            return _copy_default(default)

        try:
            raw_text = path.read_text(encoding="utf-8")
            if not raw_text.strip():
                return _copy_default(default)
            return json.loads(raw_text)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"JSON storage read failed for {name}: {exc}", flush=True)
            return _copy_default(default)

    def save_json(self, name: str, data: Any) -> None:
        path = Path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_to_pretty_json(data), encoding="utf-8")
        except OSError as exc:
            print(f"JSON storage write failed for {name}: {exc}", flush=True)


class AzureBlobJsonStorage:
    def __init__(self) -> None:
        from azure.storage.blob import BlobServiceClient

        connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        container_name = os.getenv("AZURE_STORAGE_CONTAINER", "").strip()
        if not connection_string:
            raise ValueError("AZURE_STORAGE_CONNECTION_STRING is required when STATE_BACKEND=blob")
        if not container_name:
            raise ValueError("AZURE_STORAGE_CONTAINER is required when STATE_BACKEND=blob")

        service_client = BlobServiceClient.from_connection_string(connection_string)
        self.container_client = service_client.get_container_client(container_name)

    def load_json(self, name: str, default: Any) -> Any:
        blob_client = self.container_client.get_blob_client(name)
        try:
            raw_bytes = blob_client.download_blob().readall()
            raw_text = raw_bytes.decode("utf-8", errors="replace")
            if not raw_text.strip():
                self.save_json(name, default)
                return _copy_default(default)
            return json.loads(raw_text)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"Blob JSON read failed for {name}: {exc}", flush=True)
            return _copy_default(default)
        except Exception as exc:
            if exc.__class__.__name__ == "ResourceNotFoundError":
                self.save_json(name, default)
                return _copy_default(default)
            print(f"Blob JSON read failed for {name}: {exc.__class__.__name__}", flush=True)
            return _copy_default(default)

    def save_json(self, name: str, data: Any) -> None:
        blob_client = self.container_client.get_blob_client(name)
        try:
            blob_client.upload_blob(_to_pretty_json(data), overwrite=True)
        except Exception as exc:
            print(f"Blob JSON write failed for {name}: {exc.__class__.__name__}", flush=True)


def get_storage() -> LocalJsonStorage | AzureBlobJsonStorage:
    if os.getenv("STATE_BACKEND", "").strip().lower() == "blob":
        try:
            return AzureBlobJsonStorage()
        except Exception as exc:
            print(f"Azure Blob storage initialization failed: {exc}", flush=True)
            return LocalJsonStorage()
    return LocalJsonStorage()


def _copy_default(default: Any) -> Any:
    try:
        return json.loads(json.dumps(default))
    except TypeError:
        return default


def _to_pretty_json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
