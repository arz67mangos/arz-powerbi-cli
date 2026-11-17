"""Session: open PBIP project + undo/redo, and the persisted default
workspace/dataset context (~/.cli-anything-powerbi/session.json)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from cli_anything.powerbi.core.project import Project


def state_dir() -> Path:
    d = Path(os.environ.get("CLI_ANYTHING_POWERBI_HOME", Path.home() / ".cli-anything-powerbi"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _locked_save_json(path, data, **dump_kwargs) -> None:
    """Atomically write JSON with exclusive file locking."""
    try:
        f = open(path, "r+")
    except FileNotFoundError:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        f = open(path, "w")
    with f:
        _locked = False
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            _locked = True
        except (ImportError, OSError):
            pass
        try:
            f.seek(0)
            f.truncate()
            json.dump(data, f, **dump_kwargs)
            f.flush()
        finally:
            if _locked:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class Session:
    def __init__(self):
        self.project: Project | None = None
        self.project_path: str | None = None
        self._modified = False
        self._undo: list = []
        self._redo: list = []

    # ── project ──
    def open(self, path: str) -> Project:
        self.project, self.project_path = Project(path), path
        self._modified = False
        self._undo.clear()
        self._redo.clear()
        return self.project

    def has_project(self) -> bool:
        return self.project is not None

    def require(self) -> Project:
        if not self.project:
            raise RuntimeError("No project open. Pass --project <file.pbip> or run 'project open'.")
        return self.project

    def snapshot(self):
        """Call before every mutation."""
        self._undo.append(self.require().snapshot())
        self._redo.clear()
        self._modified = True

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self.project.snapshot())
        self.project.restore(self._undo.pop())
        self._modified = bool(self.project.dirty)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self.project.snapshot())
        self.project.restore(self._redo.pop())
        self._modified = True
        return True

    def save_session(self) -> list[str]:
        written = self.require().save()
        self._modified = False
        return written

    # ── persisted context ──
    @staticmethod
    def context() -> dict:
        p = state_dir() / "session.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def set_context(**kw) -> dict:
        ctx = Session.context()
        for k, v in kw.items():
            if v is None:
                ctx.pop(k, None)
            else:
                ctx[k] = v
        _locked_save_json(state_dir() / "session.json", ctx, indent=2)
        return ctx
