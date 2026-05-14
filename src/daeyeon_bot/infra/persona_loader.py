"""Loader for `<skills_root>/<name>/SKILL.md` personas.

Stat-on-each-call mtime caching: if the file hasn't been touched since the
last read, the cached `Persona` is reused. Otherwise the body is re-read,
re-validated, and re-cached. See feature 001 `contracts/persona-skill-format.md` §4
and feature 002's same contract.

Default `skills_root` is the repo-bundled `.claude/skills/` directory next
to this package (resolved from `__file__`), so the shipped personas
(`daeyeon-bot-code-review`, `daeyeon-bot-jira-triage`) work out of the
box. Override via the handler-specific config knob (e.g.
`[handlers.pr_review].skills_root`) to point at `~/.claude/skills` or
anywhere else.

Failures raise `core.errors.ValidationError("persona unavailable: <reason>")`.
The handler converts that to `DeadLetter`.
"""

from __future__ import annotations

import threading
from pathlib import Path

from daeyeon_bot.core.errors import ValidationError
from daeyeon_bot.core.persona import Persona

_FRONTMATTER_DELIM = "---"

# `<repo>/src/daeyeon_bot/infra/persona_loader.py` → `<repo>/.claude/skills`.
# Editable install (`uv run`) keeps __file__ inside the source tree, so this
# resolves to the repo's bundled skills dir.
_REPO_SKILLS_ROOT = Path(__file__).resolve().parents[3] / ".claude" / "skills"


class PersonaLoader:
    """Mtime-cached SKILL.md loader. One instance per persona slot.

    A loader instance caches **one** persona at a time — the most recent
    `load(name)` result. Different handlers (pr_review, jira_triage) get
    separate loader instances so their caches don't collide.
    """

    def __init__(self, *, skills_root: Path | None = None) -> None:
        self._root = (skills_root or _REPO_SKILLS_ROOT).expanduser()
        self._cache: Persona | None = None
        self._lock = threading.Lock()

    def load(self, name: str, *, min_chars: int = 200) -> Persona:
        """Return the active Persona for `name`, validating + caching by mtime."""
        if not name:
            raise ValidationError("persona unavailable: persona_skill is empty")
        path = self._root / name / "SKILL.md"
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise ValidationError(f"persona unavailable: SKILL.md not found at {path}") from exc
        except PermissionError as exc:
            raise ValidationError(
                f"persona unavailable: cannot read SKILL.md ({exc.errno})"
            ) from exc
        except OSError as exc:
            raise ValidationError(f"persona unavailable: stat failed ({exc.errno}: {exc})") from exc

        with self._lock:
            cached = self._cache
            if (
                cached is not None
                and cached.skill_dir == path.parent
                and cached.mtime_ns == stat.st_mtime_ns
            ):
                return cached

        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(f"persona unavailable: cannot read SKILL.md ({exc})") from exc

        body = _strip_frontmatter(raw)
        _validate_body(body, min_chars=min_chars)
        persona = Persona(skill_dir=path.parent, name=name, body=body, mtime_ns=stat.st_mtime_ns)
        with self._lock:
            self._cache = persona
        return persona


def _strip_frontmatter(text: str) -> str:
    """Remove a leading `---\\n…\\n---\\n` block. Frontmatter parse errors are ignored."""
    if not text.startswith(_FRONTMATTER_DELIM + "\n") and not text.startswith(
        _FRONTMATTER_DELIM + "\r\n"
    ):
        return text
    # Find the closing delimiter on its own line.
    lines = text.splitlines(keepends=True)
    if not lines:
        return text
    closing_idx: int | None = None
    for i in range(1, len(lines)):
        stripped = lines[i].rstrip("\r\n")
        if stripped == _FRONTMATTER_DELIM:
            closing_idx = i
            break
    if closing_idx is None:
        # No closing delimiter — file is malformed; treat the whole thing as body.
        return text
    return "".join(lines[closing_idx + 1 :])


def _validate_body(body: str, *, min_chars: int) -> None:
    if len(body) < min_chars:
        raise ValidationError(
            f"persona unavailable: body too short ({len(body)} chars, min {min_chars})"
        )
    has_real_line = any(line.strip() for line in body.splitlines())
    if not has_real_line:
        raise ValidationError("persona unavailable: body is whitespace-only")


__all__ = ["PersonaLoader"]
