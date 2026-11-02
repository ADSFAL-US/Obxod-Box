"""Автоопределение версии приложения Obxod-BOX.

Правила инкремента версии (semver x.y.z):
- Прямой коммит в main: патч-версия (z+1).
- Мерж в main: минорная версия (y+1), патч сбрасывается в 0.
- Мерж в main с более чем 20 коммитами в ветке: мажорная версия (x+1), y и z сбрасываются в 0.

На клиентской машине (без .git) используется статический фолбэк __version__,
который обновляется в процессе релиза.
"""

import re
import subprocess

__version__ = "0.0.9"


def _git(cmd: str) -> str:
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.DEVNULL
        ).decode().strip()
    except Exception:
        return ""


def _parse_version(tag: str):
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return 0, 0, 0


def get_version() -> str:
    tag = _git("git describe --tags --abbrev=0")
    if not tag:
        return __version__

    x, y, z = _parse_version(tag)

    parents = _git("git log -1 --pretty=%P HEAD")
    is_merge = len(parents.split()) > 1 if parents else False

    if is_merge:
        merge_commits = _git("git rev-list --count ^HEAD^1 HEAD")
        try:
            merge_commits = int(merge_commits)
        except ValueError:
            merge_commits = 0

        if merge_commits > 20:
            x += 1
            y = 0
            z = 0
        else:
            y += 1
            z = 0
    else:
        z += 1

    return f"{x}.{y}.{z}"
