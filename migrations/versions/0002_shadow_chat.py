"""Add optional shadow chat deployments and responses.

Revision ID: 0002_shadow_chat
Revises: 0001_initial
Create Date: 2026-09-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_shadow_chat"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    turn_status = sa.Enum("pending", "completed", "failed", name="turnstatus")
    if op.get_bind().dialect.name == "postgresql":
        turn_status = postgresql.ENUM(
            "pending", "completed", "failed", name="turnstatus", create_type=False
        )
    op.create_table(
        "session_shadows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("slot", sa.String(5), nullable=False),
        sa.Column("model_deployment_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot IN ('red', 'green', 'blue')", name="ck_session_shadow_slot"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.ForeignKeyConstraint(["model_deployment_id"], ["model_deployments.id"]),
        sa.UniqueConstraint("session_id", "slot", name="uq_session_shadow_slot"),
        sa.UniqueConstraint(
            "session_id", "model_deployment_id", name="uq_session_shadow_deployment"
        ),
    )
    op.create_index("ix_session_shadows_session_id", "session_shadows", ["session_id"])
    op.create_table(
        "shadow_responses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("session_shadow_id", sa.Integer(), nullable=False),
        sa.Column("assistant_response", sa.Text()),
        sa.Column("request_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inference_started_at", sa.DateTime(timezone=True)),
        sa.Column("first_token_at", sa.DateTime(timezone=True)),
        sa.Column("response_completed_at", sa.DateTime(timezone=True)),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("time_to_first_token_ms", sa.Float()),
        sa.Column("inference_latency_ms", sa.Float()),
        sa.Column("total_latency_ms", sa.Float()),
        sa.Column("tokens_per_second", sa.Float()),
        sa.Column(
            "status",
            turn_status,
            nullable=False,
        ),
        sa.Column("error_type", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("provider_metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"]),
        sa.ForeignKeyConstraint(["session_shadow_id"], ["session_shadows.id"]),
        sa.UniqueConstraint("turn_id", "session_shadow_id", name="uq_shadow_response_turn"),
    )
    op.create_index("ix_shadow_responses_turn_id", "shadow_responses", ["turn_id"])
    op.create_index(
        "ix_shadow_responses_session_shadow_id", "shadow_responses", ["session_shadow_id"]
    )
    op.create_table(
        "eval_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(10), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('primary', 'red', 'green', 'blue')",
            name="ck_eval_candidate_source",
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"]),
        sa.UniqueConstraint("turn_id", "source", name="uq_eval_candidate_source"),
    )
    op.create_index("ix_eval_candidates_turn_id", "eval_candidates", ["turn_id"])


def downgrade() -> None:
    op.drop_table("eval_candidates")
    op.drop_table("shadow_responses")
    op.drop_table("session_shadows")
