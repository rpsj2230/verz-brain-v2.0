"""Extensions, schema namespaces, and the application role.

The first migration. It creates nothing that holds data — it establishes the ground every
later migration stands on: the four extensions, the nine schemas, and an application role
that explicitly cannot bypass row-level security.

That last one is the point of the whole file. PostgreSQL superusers, and any role with
BYPASSRLS, ignore row-level security entirely. If the application connects as one, every
policy we later write is decoration: the queries still return every row, the tests still
pass because the tests use the same role, and nobody finds out until a person sees data
they should not. Creating the role NOBYPASSRLS here, before any table exists, means that
mistake cannot be made later by accident.

Task ids: M0.3.1, M0.3.3, M0.3.7

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import os

from alembic import op
from psycopg import sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = ("auth", "gate", "agent", "know", "mem", "obs", "proj", "er", "ops")
EXTENSIONS = ("vector", "pg_trgm", "fuzzystrmatch", "unaccent")

APP_ROLE = "brain_app"


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')

    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # The application role. NOBYPASSRLS is the whole reason this migration exists; it is
    # spelled out rather than left to the default so that reading this file answers the
    # question "can the app see past row-level security?" without checking pg_roles.
    #
    # The role name is a constant in this file, so interpolating it is safe. The password
    # is handled separately below, because it is not.
    op.execute(  # noqa: S608 - APP_ROLE is a constant in this file, not input
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOINHERIT NOBYPASSRLS;
            END IF;
            ALTER ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS;
        END
        $$;
        """
    )

    password = os.environ.get("APP_ROLE_PASSWORD", "")
    if password:
        # The password is the one value here that is not ours, so it is never pasted into
        # SQL by hand. PostgreSQL accepts no bind parameters in DDL, and none at all
        # inside a DO block, so psycopg's own quoting builds the statement instead.
        #
        # Two dead ends are recorded here because both look correct and neither is: bind
        # parameters inside DO fail outright, and SELECT format('... %L', :pw) fails too,
        # first because format() takes variadic "any" and cannot infer a parameter type,
        # then because a ::text cast collides with SQLAlchemy's own :param syntax.
        #
        # This matters for a password with a quote in it, which any generator produces
        # eventually — and the difference is between it failing loudly and it executing.
        conn = op.get_bind()
        stmt = (
            sql.SQL("ALTER ROLE {role} PASSWORD {pw}")
            .format(role=sql.Identifier(APP_ROLE), pw=sql.Literal(password))
            .as_string(conn.connection.driver_connection)
        )
        conn.exec_driver_sql(stmt)

    for schema in SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE}")
        # Table privileges are granted per table as tables arrive, not blanket here.
        # A default privilege that grants SELECT on everything future would quietly
        # include tables whose classification nobody has decided yet.

    # The fast lane answers without a model, from the local projection only, and must be
    # unable to reach the network. That is enforced by the database rather than by a
    # prompt: this role holds no privileges beyond reading projected fields.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'brain_fastlane') THEN
                CREATE ROLE brain_fastlane NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT brain_fastlane TO {APP_ROLE}")
    op.execute("GRANT USAGE ON SCHEMA proj TO brain_fastlane")


def downgrade() -> None:
    op.execute("REVOKE USAGE ON SCHEMA proj FROM brain_fastlane")
    op.execute(f"REVOKE brain_fastlane FROM {APP_ROLE}")
    for schema in SCHEMAS:
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {APP_ROLE}")
    # Roles are not dropped: another database on the same cluster may hold objects owned
    # by them, and DROP ROLE would fail or orphan those. Reversing this migration returns
    # the schema to its previous state, not the cluster.
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    # Extensions are left in place for the same reason.
