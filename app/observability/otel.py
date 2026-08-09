"""NFR-OBS-01 (distributed tracing, request path + arq worker paths) and the
tracer/meter this package's other modules (app/capture/health.py's
NFR-OBS-02 gauge, app/observability/drift.py's job spans) share.

Standard `OTEL_*` env vars, not CUE_-prefixed: that's what lets the
instrumentation libraries below (opentelemetry-instrumentation-fastapi/
-httpx/-sqlalchemy) and the OTLP exporters pick up configuration with zero
custom plumbing — same reason the pricing/config layers in this codebase
prefer standard shapes over bespoke ones where one already exists.

No-op posture, mirroring app/llm/factory.py's stance toward missing
credentials (and explicitly required by Prompt 13's own "non-obvious
things" note): if `OTEL_EXPORTER_OTLP_ENDPOINT` isn't set,
`configure_otel()` does nothing — no SDK provider installed, no exporter
thread started, no network call ever attempted, and critically, no
instrumentation library patches anything (FastAPI routing, httpx clients,
the SQLAlchemy engine) either. That last point matters beyond overhead: the
test suite runs with no OTEL endpoint configured and several tests exercise
httpx/SQLAlchemy behaviour directly — global monkeypatching from an
instrumentation library must not be active during `uv run pytest` at all,
not just "a cheap no-op."

`get_tracer()`/`get_meter()` are always safe to call regardless of whether
`configure_otel()` ran or found itself unconfigured — they return the
OTel API's own default no-op tracer/meter in that case, so callers (arq job
bodies, the capture-health gauge) never need an "is this configured" check
of their own.
"""

import logging
import os

from opentelemetry import metrics, trace

logger = logging.getLogger("cue.observability")

_configured = False


def configure_otel(service_name: str, *, app=None, engine=None) -> bool:
    """Call once per process: `main.py` (FastAPI, pass `app`) and
    `app/foresight/worker.py` (arq, pass `engine` — no FastAPI app exists
    there). Safe to call more than once; second call is a no-op. Returns
    whether tracing/metrics actually got configured (tests assert on this
    to prove the no-op path rather than asserting on absence of a crash)."""
    global _configured
    if _configured:
        return False
    _configured = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set — tracing/metrics disabled (no-op)")
        return False

    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({SERVICE_NAME: service_name})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    meter_provider = MeterProvider(
        resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())]
    )
    metrics.set_meter_provider(meter_provider)

    # httpx: covers OllamaClient/AnthropicClient (app/llm/client.py) and
    # every channel adapter's outbound call in one place, rather than
    # instrumenting each caller individually.
    HTTPXClientInstrumentor().instrument()

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)

    if engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)

    logger.info("OpenTelemetry configured: service=%s endpoint=%s", service_name, endpoint)
    return True


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    return metrics.get_meter(name)


def traced_job(fn):
    """Wraps an arq job coroutine so its whole body runs inside one span
    (`job.<function name>`) — a manual wrapper because the arq worker
    process has no FastAPI request layer to auto-instrument the request
    path the way main.py's HTTP routes get for free from
    FastAPIInstrumentor. `functools.wraps` preserves `__name__`/
    `__doc__` so arq's by-name job dispatch and this module's own
    docstrings-as-documentation convention both still work on the wrapped
    function exactly as they did on the original."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with get_tracer("cue.arq").start_as_current_span(f"job.{fn.__name__}"):
            return await fn(*args, **kwargs)

    return wrapper
