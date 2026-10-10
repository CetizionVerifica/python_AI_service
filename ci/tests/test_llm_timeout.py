"""LLM calls are bounded by LLM_TIMEOUT_S (F-14): a hung OpenRouter request
fails within the limit instead of holding a worker forever."""
import socket
import threading
import time

import pytest


def test_client_uses_configured_knobs():
    from app.core.config import settings
    from app.services import llm

    assert llm.client.timeout == settings.LLM_TIMEOUT_S
    assert llm.client.max_retries == 0  # retries happen once, in tenacity
    assert llm._call_openrouter.retry.stop.max_attempt_number == settings.MAX_RETRIES + 1


@pytest.fixture
def hung_server():
    """Accepts connections and never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                continue

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join()
    for c in held:
        c.close()
    srv.close()


def test_hung_call_times_out(hung_server, monkeypatch):
    import httpx

    from app.core.config import settings
    from app.services import llm

    monkeypatch.setattr(settings, "LLM_TIMEOUT_S", 0.5)
    monkeypatch.setattr(
        llm, "client",
        llm.client.with_options(
            base_url=f"http://127.0.0.1:{hung_server}/v1",
            http_client=httpx.Client(trust_env=False),
        ),
    )
    started = time.monotonic()
    with pytest.raises(Exception) as exc:
        llm._call_openrouter.__wrapped__([{"role": "user", "content": "hi"}])
    assert time.monotonic() - started < 5
    assert "timed out" in str(exc.value).lower() or "timeout" in type(exc.value).__name__.lower()


def test_budgeted_call_counts_the_slot_wait(hung_server, monkeypatch):
    """Column inference answers within its budget even when it first waits for
    a slot, so ESG-lite (30 s) is never left waiting on work nobody reads."""
    import httpx

    from app.services import llm

    monkeypatch.setattr(
        llm, "client",
        llm.client.with_options(
            base_url=f"http://127.0.0.1:{hung_server}/v1",
            http_client=httpx.Client(trust_env=False),
        ),
    )
    monkeypatch.setattr(llm, "_llm_slots", threading.BoundedSemaphore(1))

    # Every slot busy: the call gives up within the budget.
    llm._llm_slots.acquire()
    started = time.monotonic()
    with pytest.raises(llm.LLMError):
        llm.call_openrouter_within([{"role": "user", "content": "hi"}], 0.5)
    assert time.monotonic() - started < 2

    # Slot freed after part of the budget: the request gets only what is left.
    threading.Timer(0.4, llm._llm_slots.release).start()
    started = time.monotonic()
    with pytest.raises(Exception):
        llm.call_openrouter_within([{"role": "user", "content": "hi"}], 2.0)
    assert time.monotonic() - started < 3


def test_column_inference_routes_run_off_the_event_loop():
    import inspect

    from app.api import column_config

    assert not inspect.iscoroutinefunction(column_config.infer_columns)
    assert not inspect.iscoroutinefunction(column_config.infer_all_columns)


def test_factor_upload_runs_category_inference_off_the_event_loop():
    import inspect

    from app.api import emission_factors

    src = inspect.getsource(emission_factors)
    assert "= infer_category_mapping(" not in src
    assert src.count("run_in_threadpool(\n                    infer_category_mapping") == 2
