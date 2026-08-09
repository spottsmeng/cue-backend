"""app/observability/otel.py — NFR-OBS-01's no-op-when-unconfigured
posture, the property Prompt 13's own "non-obvious things" note is most
explicit about: instrumentation must not require OTEL_EXPORTER_OTLP_ENDPOINT
to be set for `uv run pytest`'s own request-path/arq-job tests to keep
passing untouched. The full suite already proves this transitively (OTEL_*
is never set anywhere in the test env) — this file makes that guarantee
explicit and tests configure_otel/traced_job's own return contracts
directly, rather than relying on absence-of-crash elsewhere.
"""

import os

import pytest

from app.observability.otel import configure_otel, get_meter, get_tracer, traced_job


def test_configure_otel_is_a_no_op_when_endpoint_unset(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import app.observability.otel as otel_module

    otel_module._configured = False  # this module-level singleton persists across tests otherwise
    configured = configure_otel("test-service")
    assert configured is False


def test_configure_otel_is_idempotent(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import app.observability.otel as otel_module

    otel_module._configured = False
    first = configure_otel("test-service")
    second = configure_otel("test-service")
    assert first is False
    assert second is False  # already-configured short-circuit, not a second no-op decision


def test_get_tracer_and_get_meter_are_always_usable():
    """Safe to call regardless of whether configure_otel ever ran or found
    itself unconfigured — no exception, no network call, a real (if no-op)
    span/instrument context manager."""
    tracer = get_tracer("test")
    with tracer.start_as_current_span("test-span"):
        pass

    meter = get_meter("test")
    gauge = meter.create_gauge("test.metric")
    gauge.set(1, attributes={"a": "b"})  # must not raise


@pytest.mark.asyncio
async def test_traced_job_wraps_and_calls_through_preserving_name():
    async def my_job(ctx=None):
        return "job ran"

    wrapped = traced_job(my_job)
    assert wrapped.__name__ == "my_job"
    result = await wrapped()
    assert result == "job ran"
