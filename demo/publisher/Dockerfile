# The publishing participant.
#
# Two properties matter more than anything else, and they are the same two
# the package itself claims:
#
#   1. No third-party dependencies. There is no `pip install` step, so there
#      is nothing to pin, mirror, or trust beyond the base image digest, and
#      the build is hermetic. That is not a container convenience — it is the
#      package's central claim, tested here across a real image boundary.
#   2. The core arrives through the `hb_core` build context as a plain
#      `mcp_heartbeat` package, with no wheel, no install, and no repository
#      around it. If the core needed anything from the tree it was assembled
#      in, this image would not start.
#
# Base image pinned by digest so a rebuild months from now is the same build.
# Refresh deliberately, never implicitly.
FROM python:3.12-slim@sha256:dd29372629eeba2dd003fd9e9d35a5b8236c44727875a0364254b5127af88e65

# No .pyc writes: the container runs with a read-only root filesystem.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin demo

WORKDIR /app

# The core, lifted out of the repository as a standalone package.
COPY --from=hb_core . /app/mcp_heartbeat/
COPY publisher/hb_demo_publisher /app/hb_demo_publisher/

# Build contexts carry whatever was on disk; drop compiled leftovers so the
# image contains only source. The import check is the hermeticity proof: it
# runs with nothing installed but the standard library.
RUN find /app -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && python -c "import mcp_heartbeat, hb_demo_publisher; \
print('core:', mcp_heartbeat.__version__, 'layout:', hb_demo_publisher.LAYOUT)"

USER 10001:10001
EXPOSE 8981

# Liveness of the process only — and the endpoint says so in its own body.
# The authoritative answer lives in the heartbeat document, and conflating
# the two is the mistake this demonstration exists to catch.
HEALTHCHECK --interval=3s --timeout=3s --start-period=2s --retries=15 \
    CMD ["python", "-c", "import os,sys,urllib.request;\
url='http://127.0.0.1:'+os.environ.get('HB_DEMO_PORT','8981')+'/healthz';\
sys.exit(0 if urllib.request.urlopen(url, timeout=2).status==200 else 1)"]

CMD ["python", "-m", "hb_demo_publisher"]
