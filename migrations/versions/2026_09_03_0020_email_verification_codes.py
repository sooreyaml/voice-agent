"""Switch email verification from a link token to a short numeric code.

Verification now emails a 6-digit code the user types back in. A code is
guessable in a way a 32-byte link token is not, so each row gains an
``attempts`` counter (the code locks after a handful of wrong tries) and the
unique constraint on ``token_hash`` is dropped -- two users can be issued the
same code, so its hash is no longer globally unique. Password-reset tokens are
unaffected; they still ride the same table as opaque link tokens.

Revision ID: 202609030020
Revises: 202609010019
Create Date: 2026-09-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202609030020"
down_revision: str | Sequence[str] | None = "202609010019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("email_tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.drop_constraint(
            "uq_email_tokens_token_hash", type_="unique"
        )


def downgrade() -> None:
    with op.batch_alter_table("email_tokens") as batch_op:
        batch_op.create_unique_constraint(
            "uq_email_tokens_token_hash", ["token_hash"]
        )
        batch_op.drop_column("attempts")
