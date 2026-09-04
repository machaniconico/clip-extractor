"""Atomic local secret storage with Windows DPAPI protection."""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import NoReturn


logger = logging.getLogger("clip-extractor.secret-store")

_MAGIC_PREFIX = "CLIPSEC1:"
_APPLICATION_ENTROPY = b"clip-extractor.secret-store.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class SecretUnavailableError(RuntimeError):
    """Raised when a stored secret exists but cannot be read safely."""


def _log_secret_failure(operation: str, path: Path, error: Exception) -> None:
    """Log actionable context without emitting secret-bearing error text."""
    logger.warning(
        "Secret %s failed for %s (error_type=%s)",
        operation,
        path,
        type(error).__name__,
    )


def _raise_secret_unavailable() -> NoReturn:
    raise SecretUnavailableError("Stored secret is unavailable") from None


def is_encryption_available() -> bool:
    """Return whether this platform provides the Windows DPAPI backend."""
    return sys.platform == "win32"


def _dpapi_transform(data: bytes, *, protect: bool) -> bytes:
    """Protect or unprotect bytes with user-scoped Windows DPAPI."""
    # Keep ctypes and the Windows DLLs out of non-Windows import paths.
    import ctypes
    from ctypes import wintypes

    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    def _blob(value: bytes):
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        blob = _DATA_BLOB(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        return blob, buffer

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_APPLICATION_ENTROPY)
    output_blob = _DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

    blob_pointer = ctypes.POINTER(_DATA_BLOB)
    if protect:
        operation = crypt32.CryptProtectData
        operation.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
    else:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            blob_pointer,
        ]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    # The buffer references must remain alive through the native call.
    _ = input_buffer, entropy_buffer
    try:
        succeeded = operation(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            raise RuntimeError("DPAPI operation failed")
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(
                ctypes.cast(output_blob.pbData, ctypes.c_void_p)
            )


def _protect_data(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=True)


def _unprotect_data(data: bytes) -> bytes:
    return _dpapi_transform(data, protect=False)


def _atomic_write(path: Path, payload: bytes) -> None:
    file_descriptor: int | None = None
    temporary_name: str | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        handle = os.fdopen(file_descriptor, "wb")
        file_descriptor = None
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except Exception:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise


def read_secret_text(path: Path) -> str:
    """Read a secret, returning empty only when the file does not exist."""
    path = Path(path)
    try:
        stored_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as error:
        _log_secret_failure("read", path, error)
        _raise_secret_unavailable()

    if stored_text.startswith(_MAGIC_PREFIX):
        if not is_encryption_available():
            logger.warning(
                "Secret decrypt failed for %s (error_type=BackendUnavailable)",
                path,
            )
            _raise_secret_unavailable()
        try:
            encoded_blob = stored_text[len(_MAGIC_PREFIX):].encode("ascii")
            protected = base64.b64decode(encoded_blob, validate=True)
        except Exception as error:
            _log_secret_failure("envelope decode", path, error)
            _raise_secret_unavailable()
        try:
            decrypted = _unprotect_data(protected)
        except Exception as error:
            _log_secret_failure("decrypt", path, error)
            _raise_secret_unavailable()
        try:
            return decrypted.decode("utf-8")
        except (AttributeError, UnicodeError) as error:
            _log_secret_failure("decrypted payload decode", path, error)
            _raise_secret_unavailable()

    if is_encryption_available():
        try:
            write_secret_text(path, stored_text)
        except Exception as error:
            _log_secret_failure("legacy plaintext migration", path, error)
    return stored_text


def write_secret_text(path: Path, text: str) -> None:
    """Write a secret atomically, encrypting it with DPAPI on Windows."""
    path = Path(path)
    try:
        if is_encryption_available():
            protected = _protect_data(text.encode("utf-8"))
            stored_text = _MAGIC_PREFIX + base64.b64encode(protected).decode("ascii")
        else:
            stored_text = text
        _atomic_write(path, stored_text.encode("utf-8"))
    except Exception as error:
        _log_secret_failure("write", path, error)
        raise RuntimeError("Secret storage operation failed") from None


def delete_secret(path: Path) -> None:
    """Delete a stored secret, doing nothing when it is already absent."""
    path = Path(path)
    try:
        path.unlink(missing_ok=True)
    except Exception as error:
        _log_secret_failure("delete", path, error)
        raise RuntimeError("Secret storage operation failed") from None
