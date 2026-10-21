import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scoring"))  # poolseed (shared pool builder)
sys.path.insert(0, str(HERE.parent / "llm"))


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    import httpx

    from newsd.llm import keys

    def refuse(request):
        raise AssertionError(f"unexpected real model call to {request.url}")

    monkeypatch.setattr(keys, "TRANSPORT", httpx.MockTransport(refuse))
