"""Shared setup for the CI safety tests.

Database tests run only against the disposable Postgres container the CI
workflow starts. The guard below refuses anything else, so these tests can
never reach production even if someone runs them with a real .env around.
"""
import json
import os
import pathlib

import pytest

# Settings() requires an OpenRouter key at import time; CI never calls the LLM.
os.environ.setdefault("OPENROUTER_API_KEY", "ci-dummy-not-a-real-key")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SNAPSHOT_DIR = ROOT / "ci" / "snapshots"
FIXTURES = ROOT / "ci" / "fixtures"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def throwaway_db_problems() -> list[str]:
    problems = []
    if os.environ.get("CI_THROWAWAY_DB") != "true":
        problems.append("CI_THROWAWAY_DB is not 'true'")
    if os.environ.get("DB_HOST") not in LOCAL_HOSTS:
        problems.append(f"DB_HOST {os.environ.get('DB_HOST')!r} is not a local throwaway host")
    if not str(os.environ.get("DB_NAME", "")).startswith("ci_"):
        problems.append(f"DB_NAME {os.environ.get('DB_NAME')!r} does not start with ci_")
    return problems


@pytest.fixture(scope="session")
def throwaway_db():
    """A fresh copy of the ESG-lite schema plus the fixed reference data."""
    problems = throwaway_db_problems()
    if problems:
        if os.environ.get("CI") == "true":
            pytest.fail("Refusing to touch this database: " + "; ".join(problems))
        pytest.skip("no throwaway database: " + "; ".join(problems))

    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        user=os.environ.get("DB_USERNAME", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
        dbname=os.environ["DB_NAME"],
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        cur.execute((FIXTURES / "esg_lite_schema.sql").read_text())
        cur.execute((FIXTURES / "reference_data.sql").read_text())
    yield conn
    conn.close()


def _normalize(value):
    if isinstance(value, dict):
        return {k: _normalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


@pytest.fixture
def snapshot():
    """Compare a JSON-able result with ci/snapshots/<name>.json.

    UPDATE_SNAPSHOT=1 rewrites the file. Do that only for a deliberate
    calculation change, and list every moved figure in the pull request.
    """

    def check(name: str, actual):
        path = SNAPSHOT_DIR / f"{name}.json"
        text = json.dumps(_normalize(actual), indent=2, ensure_ascii=False, default=str) + "\n"
        if os.environ.get("UPDATE_SNAPSHOT") == "1":
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            return
        assert path.exists(), f"{path.name} missing; run with UPDATE_SNAPSHOT=1"
        expected = path.read_text()
        if expected != text:
            (ROOT / f"{name}.actual.json").write_text(text)
        assert expected == text, (
            f"Calculation results changed ({path.name}). If deliberate, rerun with "
            f"UPDATE_SNAPSHOT=1, commit the snapshot and explain every moved figure."
        )

    return check
