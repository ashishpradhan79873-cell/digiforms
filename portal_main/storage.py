from __future__ import annotations

from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage, default_storage


class HybridMediaStorage(Storage):
    """
    Cloudinary-first storage with safe local fallback.

    Why:
    - Some hosts/network setups intermittently block api.cloudinary.com which causes 500s.
    - This keeps uploads working by falling back to local MEDIA_ROOT when Cloudinary fails.

    Behavior:
    - Save tries cloud backend; on any exception, saves to local.
    - Local files are stored under MEDIA_ROOT (with a "local/" prefix in the path).
    """

    def __init__(self, *args, **kwargs):
        super().__init__()
        self._local = FileSystemStorage(location=settings.MEDIA_ROOT, base_url=settings.MEDIA_URL)
        self._cloud = None
        try:
            from cloudinary_storage.storage import MediaCloudinaryStorage

            self._cloud = MediaCloudinaryStorage()
        except Exception:
            self._cloud = None

    def _is_local_name(self, name: str) -> bool:
        return (name or "").startswith("local/")

    def _cloud_backend(self):
        return self._cloud

    def _local_backend(self):
        return self._local

    def _save(self, name, content):
        # Prefer cloud if available; fall back to local on any error.
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.save(name, content)
            except Exception:
                pass
        safe_name = name or getattr(content, "name", "") or "upload.bin"
        if not safe_name.startswith("local/"):
            safe_name = "local/" + safe_name.lstrip("/")
        return self._local_backend().save(safe_name, content)

    def save(self, name, content, max_length=None):
        return self._save(name, content)

    def exists(self, name):
        if not name:
            return False
        if self._is_local_name(name):
            return self._local_backend().exists(name)
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.exists(name)
            except Exception:
                return self._local_backend().exists("local/" + name.lstrip("/"))
        return self._local_backend().exists("local/" + name.lstrip("/"))

    def open(self, name, mode="rb"):
        if self._is_local_name(name):
            return self._local_backend().open(name, mode)
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.open(name, mode)
            except Exception:
                return self._local_backend().open("local/" + name.lstrip("/"), mode)
        return self._local_backend().open("local/" + name.lstrip("/"), mode)

    def delete(self, name):
        if not name:
            return
        if self._is_local_name(name):
            return self._local_backend().delete(name)
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.delete(name)
            except Exception:
                return self._local_backend().delete("local/" + name.lstrip("/"))
        return self._local_backend().delete("local/" + name.lstrip("/"))

    def size(self, name):
        if self._is_local_name(name):
            return self._local_backend().size(name)
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.size(name)
            except Exception:
                return self._local_backend().size("local/" + name.lstrip("/"))
        return self._local_backend().size("local/" + name.lstrip("/"))

    def url(self, name):
        if not name:
            return ""
        if self._is_local_name(name):
            return self._local_backend().url(name)
        cloud = self._cloud_backend()
        if cloud is not None:
            try:
                return cloud.url(name)
            except Exception:
                return self._local_backend().url("local/" + name.lstrip("/"))
        return self._local_backend().url("local/" + name.lstrip("/"))

