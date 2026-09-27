"""Проверка обновлений программы: последний релиз на GitHub.

Раз в 12 часов (или по кнопке) программа спрашивает GitHub, какая версия
последняя. Если новее — в боковой панели появляется плашка «Вышла версия X».
Без интернета проверка молча пропускается. Ничего не скачивается само.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

from . import branding

log = logging.getLogger("russificator.updates")

API = f"https://api.github.com/repos/{branding.REPO}/releases/latest"
INTERVAL = 12 * 3600


def parse_version(v: str) -> Tuple[int, ...]:
    nums = re.findall(r"\d+", (v or "").split("-")[0].split("+")[0])
    return tuple(int(x) for x in nums[:4]) or (0,)


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def latest_release(timeout: float = 8) -> Optional[Dict[str, Any]]:
    from . import net
    data = net.get_json(API, headers={"Accept": "application/vnd.github+json"}, timeout=timeout)
    if not isinstance(data, dict) or not data.get("tag_name"):
        return None
    asset = next((a for a in data.get("assets") or [] if str(a.get("name", "")).endswith(".zip")), None)
    return {"version": str(data["tag_name"]).lstrip("vV"), "url": data.get("html_url") or branding.RELEASES_URL,
            "download": (asset or {}).get("browser_download_url", ""), "notes": str(data.get("body") or "")[:1500],
            "name": str(data.get("name") or data["tag_name"])}


def check(cfg: Dict[str, Any], current: str, force: bool = False) -> Dict[str, Any]:
    """{"available": bool, "version", "url", …}; результат кэшируется в настройках (update_*)."""
    cached = cfg.get("update_latest") or {}
    fresh = time.time() - float(cfg.get("update_checked_at") or 0) < INTERVAL
    if not force and (fresh or cfg.get("check_updates") is False):
        info = dict(cached)
    else:
        try:
            info = latest_release() or {}
            cfg["update_latest"] = info
            cfg["update_checked_at"] = time.time()
        except Exception as exc:  # noqa: BLE001
            log.info("проверка обновлений: %s", exc)
            info = dict(cached)
            info["error"] = str(exc)
    info["available"] = bool(info.get("version")) and is_newer(str(info.get("version")), current)
    info["current"] = current
    return info
