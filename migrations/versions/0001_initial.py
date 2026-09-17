"""Initial evaluation schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_deployments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model_id", sa.String(255), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("endpoint_reference", sa.String(1000)),
        sa.Column("configuration_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("active", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_model_deployments_active", "model_deployments", ["active"])
    op.create_index(
        "uq_active_model_deployment",
        "model_deployments",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active IS TRUE"),
        sqlite_where=sa.text("active = 1"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("anonymous_tester_id", sa.String(255)),
        sa.Column("model_deployment_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("client_metadata", sa.JSON()),
        sa.ForeignKeyConstraint(["model_deployment_id"], ["model_deployments.id"]),
    )
    op.create_index("ix_sessions_anonymous_tester_id", "sessions", ["anonymous_tester_id"])
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("turn_number", sa.Integer(), nullable=False),
        sa.Column("user_message", sa.Text(), nullable=False),
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
        sa.Column("model_deployment_id", sa.Integer(), nullable=False),
        sa.Column(
            "status", sa.Enum("pending", "completed", "failed", name="turnstatus"), nullable=False
        ),
        sa.Column("error_type", sa.String(100)),
        sa.Column("error_message", sa.Text()),
        sa.Column("provider_request_id", sa.String(255)),
        sa.Column("provider_metadata", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.ForeignKeyConstraint(["model_deployment_id"], ["model_deployments.id"]),
        sa.UniqueConstraint("session_id", "turn_number", name="uq_turn_number"),
    )
    op.create_index("ix_turns_session_id", "turns", ["session_id"])
    op.create_index(
        "uq_pending_turn_per_session",
        "turns",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.Integer()),
        sa.Column("failure_category", sa.String(100)),
        sa.Column("comment", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rating IS NULL OR rating IN (-1, 1)", name="ck_feedback_rating"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"]),
    )
    op.create_index("ix_feedback_turn_id", "feedback", ["turn_id"])


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("turns")
    op.drop_table("sessions")
    op.drop_table("model_deployments")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("DROP TYPE turnstatus"))
