import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _load_converter():
    # The script name contains hyphens, so it can't be imported normally
    spec = importlib.util.spec_from_file_location("webp_converter", os.path.join(REPO, "WebP-Image-Converter.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["webp_converter"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def conv():
    return _load_converter()


@pytest.fixture(scope="session")
def tools(conv):
    """Resolved tool paths, or skip integration tests when cwebp/exiftool are unavailable."""
    import json
    with open(os.path.join(REPO, "config.json"), encoding="utf-8") as f:
        paths = dict(conv.DEFAULT_TOOL_PATHS, **json.load(f).get("tool_paths", {}))
    resolved = conv.check_external_tools(paths)
    if not resolved:
        pytest.skip("cwebp/exiftool not available")
    return resolved
