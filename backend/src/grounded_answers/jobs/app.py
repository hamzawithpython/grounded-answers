"""
The Procrastinate App, built from a connection string (ADR 0015, ADR 0017).

The web app uses this module only to enqueue (Step 1b-ii), and it is given the app_user URL. The
worker builds the same App from WorkerSettings. Tasks are registered on the App that runs them.
"""

import procrastinate


def build_app(conninfo: str) -> procrastinate.App:
    return procrastinate.App(connector=procrastinate.PsycopgConnector(conninfo=conninfo))


def conninfo_from_sqlalchemy_url(url: str) -> str:
    """Procrastinate takes a libpq connection string, not a SQLAlchemy URL: drop the driver part."""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)
