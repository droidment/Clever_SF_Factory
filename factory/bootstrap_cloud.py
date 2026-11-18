"""Initial Cloud SQL bootstrap with a separate, non-DDL application login."""

import os

from psycopg import sql
from sqlalchemy.engine import make_url

from factory.config import settings
from factory.store import Store


def main():
    config = settings()
    owner = make_url(config.database_url)
    runtime = make_url(os.environ["FACTORY_RUNTIME_DATABASE_URL"])
    if owner.get_backend_name() != "postgresql" or runtime.username != "factory_app":
        raise ValueError("Bootstrap requires PostgreSQL and the dedicated factory_app login")
    if owner.database != runtime.database or owner.query.get("host") != runtime.query.get("host"):
        raise ValueError("Owner and application connections must target the same database")
    store = Store(config)
    try:
        store.create_schema()
        connection = store.engine.raw_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime.username,))
                if cursor.fetchone() is None:
                    cursor.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(runtime.username),
                            sql.Literal(runtime.password),
                        )
                    )
                cursor.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(owner.database),
                        sql.Identifier(runtime.username),
                    )
                )
                cursor.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime.username))
                )
                cursor.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(sql.Identifier(runtime.username))
                )
                cursor.execute(
                    sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(
                        sql.Identifier(runtime.username)
                    )
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                    ).format(sql.Identifier(runtime.username))
                )
                cursor.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}"
                    ).format(sql.Identifier(runtime.username))
                )
            connection.commit()
        finally:
            connection.close()
        print("Schema initialized and limited application login granted access")
    except Exception:
        # SQL errors may embed statements containing credentials. Do not log them.
        raise SystemExit(
            "Cloud database bootstrap failed; review connection, role ownership and grants"
        ) from None
    finally:
        store.engine.dispose()


if __name__ == "__main__":
    main()
