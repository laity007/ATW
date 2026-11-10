"""Path normalization and allow-root enforcement for evidence access."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from atw.errors import ACCESS_DENIED, UNSAFE_PATH, AtwError


def resolve_root(root: str | os.PathLike[str]) -> Path:
    return Path(root).expanduser().resolve(strict=False)


def normalize_roots(roots: Iterable[str | os.PathLike[str]]) -> list[Path]:
    out: list[Path] = []
    seen: set[str] = set()
    for raw in roots:
        if not str(raw).strip():
            continue
        resolved = resolve_root(raw)
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return out


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        pass
    # Windows comparisons are case-insensitive; normalize as a fallback.
    try:
        p = os.path.normcase(str(path))
        r = os.path.normcase(str(root)).rstrip("\\/")
        return p == r or p.startswith(r + os.sep)
    except OSError:  # pragma: no cover - defensive
        return False


def validate_read_path(
    raw: str,
    roots: Iterable[Path],
    *,
    field: str = "path",
    require_exists: bool = False,
    expect_directory: bool = False,
) -> Path:
    """Resolve an evidence path and require it to live under an allowed root.

    Symlinks are followed first: a link inside the root that points outside is
    rejected.  Missing files are allowed for log/result paths that the compute
    process may create later, unless ``require_exists`` is set.
    """
    if not raw or "\x00" in raw:
        raise AtwError(UNSAFE_PATH, f"{field} 为空或包含非法字符")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise AtwError(UNSAFE_PATH, f"{field} 必须是绝对路径: {raw}")
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise AtwError(UNSAFE_PATH, f"{field} 无法解析: {exc}") from exc

    root_list = list(roots)
    if not root_list:
        raise AtwError(UNSAFE_PATH, "未配置 allowed_read_roots，拒绝访问证据路径")
    if not any(_within(resolved, root) for root in root_list):
        raise AtwError(UNSAFE_PATH, f"{field} 不在允许读取的根目录内: {resolved}")

    if resolved.exists():
        if expect_directory and not resolved.is_dir():
            raise AtwError(ACCESS_DENIED, f"{field} 不是目录: {resolved}")
        if not expect_directory and resolved.is_dir():
            raise AtwError(ACCESS_DENIED, f"{field} 是目录而不是文件: {resolved}")
        if not os.access(resolved, os.R_OK):
            raise AtwError(ACCESS_DENIED, f"{field} 不可读（权限不足）: {resolved}")
    elif require_exists:
        raise AtwError(ACCESS_DENIED, f"{field} 不存在: {resolved}")
    return resolved
