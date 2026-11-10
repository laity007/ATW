from __future__ import annotations

import os
from pathlib import Path

import pytest

from atw.errors import ACCESS_DENIED, UNSAFE_PATH, AtwError
from atw.utils.pathsafe import normalize_roots, validate_read_path


def test_valid_path_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    f = root / "run.log"
    f.write_text("x", encoding="utf-8")
    resolved = validate_read_path(str(f), normalize_roots([root]))
    assert resolved == f.resolve()


def test_path_outside_root_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(AtwError) as exc:
        validate_read_path(str(outside), normalize_roots([root]))
    assert exc.value.code == UNSAFE_PATH


def test_relative_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(AtwError) as exc:
        validate_read_path("run.log", normalize_roots([tmp_path]))
    assert exc.value.code == UNSAFE_PATH


def test_missing_path_allowed_only_without_require_exists(tmp_path: Path) -> None:
    missing = tmp_path / "later.log"
    validate_read_path(str(missing), normalize_roots([tmp_path]))
    with pytest.raises(AtwError) as exc:
        validate_read_path(str(missing), normalize_roots([tmp_path]), require_exists=True)
    assert exc.value.code == ACCESS_DENIED


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    link = root / "link.txt"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted in this environment")
    with pytest.raises(AtwError) as exc:
        validate_read_path(str(link), normalize_roots([root]))
    assert exc.value.code == UNSAFE_PATH


def test_directory_expected(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(AtwError) as exc:
        validate_read_path(str(f), normalize_roots([tmp_path]), expect_directory=True)
    assert exc.value.code == ACCESS_DENIED
