from __future__ import annotations

import ast
from pathlib import Path

POSTGRES_IDENTIFIER_LIMIT = 63
MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"
POSITIONAL_IDENTIFIER_OPERATIONS = {
    "create_check_constraint",
    "create_foreign_key",
    "create_index",
    "create_primary_key",
    "create_unique_constraint",
    "drop_constraint",
    "drop_index",
}


def _plain_string(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _explicit_identifiers(tree: ast.AST) -> list[tuple[int, str]]:
    identifiers: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        for keyword in node.keywords:
            if keyword.arg != "name":
                continue
            identifier = _plain_string(keyword.value)
            if identifier is not None:
                identifiers.append((node.lineno, identifier))

        if not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in POSITIONAL_IDENTIFIER_OPERATIONS or not node.args:
            continue
        identifier = _plain_string(node.args[0])
        if identifier is not None:
            identifiers.append((node.lineno, identifier))
    return identifiers


def test_explicit_migration_identifiers_fit_postgres() -> None:
    oversized: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for line, identifier in _explicit_identifiers(tree):
            if len(identifier) > POSTGRES_IDENTIFIER_LIMIT:
                oversized.append(
                    f"{path.name}:{line}: {identifier!r} ({len(identifier)} chars)"
                )

    assert not oversized, (
        "Explicit migration identifiers exceed PostgreSQL's 63-character limit. "
        "Use op.f(...) for naming-convention identifiers so SQLAlchemy can "
        "truncate them deterministically:\n" + "\n".join(oversized)
    )
