# The observing participant.
#
# Same rules as the publisher image: no third-party dependencies, no wheel,
# no install step, and the core injected through the `hb_core` build context
# as a plain `mcp_heartbeat` package.
#
# The observer runs the shipped `admit()` rather than its own copy of the
# admission rules. A demonstration whose oracle re-implements the thing under
# test proves only that two copies of an idea agree, so the only judgement
# logic in this image is the core's.
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin demo

WORKDIR /app

COPY --from=hb_core . /app/mcp_heartbeat/
COPY observer/hb_demo_observer /app/hb_demo_observer/

RUN find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && python -c "import mcp_heartbeat, hb_demo_observer; \
print('core:', mcp_heartbeat.__version__, 'layout:', hb_demo_observer.LAYOUT)"

# Create the evidence mountpoint owned by the runtime user: Docker seeds a
# fresh named volume from the image directory, ownership included, so without
# this the volume lands root-owned and the non-root observer cannot write its
# record.
RUN mkdir -p /evidence && chown 10001:10001 /evidence
VOLUME ["/evidence"]

# The evidence volume is the only writable path.
USER 10001:10001

CMD ["python", "-m", "hb_demo_observer"]
