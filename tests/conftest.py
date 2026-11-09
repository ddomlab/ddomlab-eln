"""Shared setup for the tests that talk to a local copy of the ELN.

Those tests only run when ELN_TEST_URL is set, and refuse any address that isn't this computer:

    ELN_TEST_URL=http://localhost:8088 ELN_API_KEY=<a key for the copy> uv run pytest

Without ELN_TEST_URL they are skipped, and the tests in test_offline.py still run.
"""
import json
import os
import uuid
from urllib.parse import urlparse

import elabapi_python
import pytest

from ddomlab_eln import ELN


def _new_id(headers) -> int:
    return int(headers["Location"].rstrip("/").split("/")[-1])


@pytest.fixture(scope="session")
def eln():
    url = os.environ.get("ELN_TEST_URL")
    if not url:
        pytest.skip("ELN_TEST_URL isn't set, so there's no local copy to test against")
    if urlparse(url).hostname not in ("localhost", "127.0.0.1"):
        pytest.exit(f"Refusing to run tests against {url}: ELN_TEST_URL must be a copy on this computer.", returncode=2)
    return ELN(url=url)


@pytest.fixture(scope="session")
def token():
    """A word found only in this run's titles, so searches can't pick up anything else."""
    return f"pytest{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def template_id(eln, token):
    """A template with a required text field, a number with a unit, and a dropdown, in a set order."""
    api = elabapi_python.ExperimentsTemplatesApi(eln.client)
    _, _, headers = api.post_experiment_template_with_http_info(body={"title": f"{token} template"})
    new_id = _new_id(headers)
    api.patch_experiment_template(new_id, body={"metadata": json.dumps({"extra_fields": {
        "Mode": {"type": "select", "value": "fast", "options": ["fast", "slow"], "position": 3},
        "Sample ID": {"type": "text", "value": "", "required": True, "position": 1},
        "Scan rate": {"type": "number", "value": "", "unit": "deg/min", "units": ["deg/min"], "position": 2},
    }})})
    yield new_id
    api.delete_experiment_template(new_id)


@pytest.fixture(scope="session")
def resource_id(eln, token):
    api = elabapi_python.ItemsApi(eln.client)
    _, _, headers = api.post_item_with_http_info(body={"title": f"{token} resource", "category": _a_category(eln)})
    new_id = _new_id(headers)
    yield new_id
    api.delete_item(new_id)


def _a_category(eln) -> int:
    """Any resource category that exists on this copy: read from one of its resources."""
    rows = json.loads(elabapi_python.ItemsApi(eln.client).read_items(limit=50, scope=3, _preload_content=False).data)
    return next(row["category"] for row in rows if row["category"])


@pytest.fixture
def make_experiment(eln, template_id, token):
    """Creates experiments for one test, and deletes them when it ends."""
    made = []

    def make(title="run", **fields):
        fields = {"Sample ID": "S-1", **{name.replace("_", " "): value for name, value in fields.items()}}
        new_id = eln.create_experiment(template_id, f"{token} {title}", fields)
        made.append(new_id)
        return new_id

    yield make
    api = elabapi_python.ExperimentsApi(eln.client)
    for new_id in made:
        api.delete_experiment(new_id)
