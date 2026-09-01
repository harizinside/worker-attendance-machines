"""Scan jaringan LAN untuk cari mesin ZKTeco (TCP port sweep, stdlib-only)."""

import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Optional


def get_local_subnet_prefix() -> Optional[str]:
    """Deteksi prefix subnet /24 lokal (3 oktet pertama), mis. "192.168.1".

    Return None kalau gagal (mis. tidak ada koneksi jaringan).
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Trik standar: connect UDP tidak benar-benar kirim paket, cuma
        # bikin OS pilih interface/IP lokal yang dipakai buat rute keluar.
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()

    parts = local_ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3])


def _probe(ip: str, port: int, timeout: float) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sock.connect_ex((ip, port)) == 0:
            return ip
        return None
    except OSError:
        return None
    finally:
        sock.close()


def scan_port(
    subnet_prefix: str,
    port: int = 4370,
    timeout: float = 0.3,
    max_workers: int = 100,
) -> list[str]:
    """Sweep host .1-.254 di subnet_prefix, return IP yang port-nya terbuka."""
    hosts = [f"{subnet_prefix}.{i}" for i in range(1, 255)]

    open_ips: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for result in executor.map(lambda ip: _probe(ip, port, timeout), hosts):
            if result is not None:
                open_ips.append(result)

    return sorted(open_ips, key=lambda ip: int(ip.split(".")[-1]))
