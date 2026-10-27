"""
================================================================================
  VAULT I/O ENGINE - BULLETPROOF ATOMIC FILE SYSTEM FOR PRODUCT VAULT
  Guarantees: Zero-corruption, Thread-safe, Atomic Writes with .bak Recovery
================================================================================
"""

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

_VAULT_LOCK = threading.RLock()


def write_json_atomic(path: Path, data: dict, keep_backup: bool = True) -> bool:
    """
    Writes JSON data to path using an atomic swap strategy:
    1. Validates JSON serialization.
    2. Writes to temporary file on same drive/directory.
    3. Flushes and fsyncs to physical disk.
    4. Backs up existing file to .bak if requested.
    5. Replaces target file atomically with retry loop for Windows locks.
    """
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Validation before writing
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    # Fast round-trip check
    json.loads(payload)

    # 2. Write to temp file in same directory (guarantees same volume for os.replace)
    fd, tmp_path_str = tempfile.mkstemp(dir=str(path.parent), prefix="tmp_vault_", suffix=".json")
    tmp_path = Path(tmp_path_str)

    try:
        with _VAULT_LOCK:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())

            # 3. Create .bak ONLY IF current file exists and is valid JSON
            if keep_backup and path.exists():
                try:
                    # Validate that existing file is not already corrupt before copying to .bak
                    json.loads(path.read_text(encoding="utf-8"))
                    bak_path = path.with_suffix(path.suffix + ".bak")
                    shutil.copy2(path, bak_path)
                except Exception:
                    # Do not overwrite a healthy .bak with a corrupted file!
                    pass

            # 4. Atomic replace with Windows lock resilience (5 retries)
            for attempt in range(5):
                try:
                    os.replace(tmp_path, path)
                    return True
                except (PermissionError, OSError) as lock_err:
                    if attempt < 4:
                        time.sleep(0.15 * (attempt + 1))
                    else:
                        raise lock_err
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass

    return False


def read_manifest_safe(manifest_path: Path) -> dict:
    """
    Safely reads the vault manifest.
    If the manifest is corrupted, it attempts to load the .bak file.
    If no backup exists, it moves the corrupted file to .corrupt and returns an empty dict
    instead of overwriting and destroying existing records silently.
    """
    path = Path(manifest_path).resolve()
    if not path.exists():
        return {"manifest_version": "2.0", "products": {}, "categories": {}}

    with _VAULT_LOCK:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            bak_path = path.with_suffix(path.suffix + ".bak")
            corrupt_path = path.with_suffix(f".corrupt_{int(time.time())}")

            # Preserve the broken file for debugging
            try:
                path.rename(corrupt_path)
            except Exception:
                pass

            if bak_path.exists():
                try:
                    data = json.loads(bak_path.read_text(encoding="utf-8"))
                    # Re-write the recovered data as the primary manifest
                    write_json_atomic(path, data, keep_backup=True)
                    return data
                except Exception:
                    pass

            return {"manifest_version": "2.0", "products": {}, "categories": {}, "recovered_from_corruption": True}
