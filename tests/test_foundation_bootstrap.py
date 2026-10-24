import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import create_engine

import finance_app_database_service
from finance_app_database_service.database import _build_database_url
from foundation_support import ROOT


class TestDatabaseConfiguration(unittest.TestCase):
    def test_pg_credentials_are_encoded_without_changing_the_password(self):
        password = "test-only:/@?#credential"
        with patch.dict(os.environ, {
            "PGHOST": "db.test", "PGPORT": "5432", "PGDATABASE": "finance_test",
            "PGUSER": "test-user", "PGPASSWORD": password,
            "DATABASE_URL": "sqlite://",
        }, clear=True):
            url = make_url(_build_database_url())
        self.assertEqual(url.password, password)
        self.assertEqual(url.host, "db.test")
        self.assertEqual(url.database, "finance_test")

    def test_explicit_database_url_works_without_pg_password(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite://"}, clear=True):
            self.assertEqual(_build_database_url(), "sqlite://")

    def test_default_connection_contains_no_embedded_password(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(make_url(_build_database_url()).password)


class TestRuntimeMigrationLayout(unittest.TestCase):
    def test_migrations_run_from_the_docker_runtime_directory_layout(self):
        with tempfile.TemporaryDirectory(prefix="finance-api-runtime-") as directory:
            root = Path(directory)
            package = Path(finance_app_database_service.__file__).resolve().parent
            ignore = shutil.ignore_patterns("__pycache__")
            shutil.copytree(package, root / "finance_app_database_service", ignore=ignore)
            shutil.copytree(ROOT / "migrations", root / "migrations", ignore=ignore)
            shutil.copy2(ROOT / "alembic.ini", root / "alembic.ini")
            url = "sqlite:///" + str(root / "runtime.sqlite")
            environment = os.environ.copy()
            for key in ("PGPASSWORD", "PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PYTHONPATH"):
                environment.pop(key, None)
            environment["DATABASE_URL"] = url
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "-c", str(root / "alembic.ini"), "upgrade", "head"],
                cwd=root, env=environment, capture_output=True, text=True, timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            engine = create_engine(url)
            try:
                self.assertIn("guest_session", inspect(engine).get_table_names())
                with engine.connect() as connection:
                    self.assertEqual(connection.execute(text("SELECT count(*) FROM ruleset")).scalar(), 1)
            finally:
                engine.dispose()
