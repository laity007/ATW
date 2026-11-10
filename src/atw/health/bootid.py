"""Best-effort boot identity used to distinguish service restart from reboot."""

from __future__ import annotations

import hashlib
import os
import socket
from dataclasses import dataclass

import psutil


@dataclass(frozen=True, slots=True)
class BootIdentity:
    boot_id: str
    confidence: str
    detail: str


def _machine_guid_windows() -> str | None:
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value)
    except Exception:
        return None


def current_boot_identity() -> BootIdentity:
    boot_time = int(psutil.boot_time())
    if os.name != "nt":
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
                boot_id = handle.read().strip()
            if boot_id:
                return BootIdentity(boot_id, "high", "Linux /proc boot_id")
        except OSError:
            pass
        # Fallback: hostname + boot second.  Stable within one boot.
        raw = f"{socket.gethostname()}:{boot_time}"
        return BootIdentity(
            hashlib.sha256(raw.encode()).hexdigest(), "medium", "hostname + psutil.boot_time"
        )

    guid = _machine_guid_windows()
    if guid:
        raw = f"{guid}:{boot_time}"
        return BootIdentity(
            hashlib.sha256(raw.encode()).hexdigest(),
            "high",
            "Windows MachineGuid + psutil.boot_time",
        )
    raw = f"{socket.gethostname()}:{boot_time}"
    return BootIdentity(
        hashlib.sha256(raw.encode()).hexdigest(), "low", "hostname + psutil.boot_time"
    )
