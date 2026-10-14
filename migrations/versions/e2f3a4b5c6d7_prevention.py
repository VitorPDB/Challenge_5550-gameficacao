"""Prevention configuration, verified team missions and isolated scenario runs."""
from alembic import op
import sqlalchemy as sa

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cameras") as batch:
        batch.add_column(sa.Column("prevention_json", sa.JSON(), nullable=True))
    op.create_table("prevention_missions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("camera_id", sa.Integer(), nullable=False, index=True),
        sa.Column("team", sa.String(120), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("evidence", sa.String(1000)),
        sa.Column("created_by", sa.Integer()), sa.Column("submitted_by", sa.Integer()),
        sa.Column("verified_by", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True)))
    op.create_table("scenario_runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("camera_id", sa.Integer(), nullable=False, index=True),
        sa.Column("owner_id", sa.Integer()), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_json", sa.JSON()), sa.Column("error", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("scenario_runs")
    op.drop_table("prevention_missions")
    with op.batch_alter_table("cameras") as batch:
        batch.drop_column("prevention_json")
