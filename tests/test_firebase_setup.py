"""cloud/firebase_setup.py names no project of its own: it takes --project or the app's
google-services.json."""
import importlib.util
import json
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parent.parent / "cloud" / "firebase_setup.py"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("firebase_setup", SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "GOOGLE_SERVICES", tmp_path / "google-services.json")
    return mod


def test_the_project_comes_from_the_flag_or_the_apps_config(setup):
    with pytest.raises(setup.Failed, match="no --project"):
        setup.project_id(None)
    setup.GOOGLE_SERVICES.write_text(json.dumps({"project_info": {"project_id": "my-app-123"}}))
    assert setup.project_id(None) == "my-app-123"
    assert setup.project_id("other-456") == "other-456"
    assert "PROJECT" not in vars(setup)
