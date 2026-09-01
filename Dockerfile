# Official Python 3.11 slim-bookworm multi-architecture manifest, pinned after
# verification. Update deliberately as part of a reviewed release.
FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Keep the runtime process unprivileged.  The numeric IDs also make ownership
# predictable when the image is run by an orchestrator that does not know the
# image's user name.
RUN groupadd --system --gid 10001 tradingbot \
    && useradd --system --uid 10001 --gid tradingbot --home-dir /app --shell /usr/sbin/nologin tradingbot

# The lock is resolved for Python 3.11 and pins every transitive runtime
# package. Regenerate it deliberately with the documented uv command whenever
# requirements.txt changes; do not make production image builds resolve ranges.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m pip check

COPY --chown=tradingbot:tradingbot . ./

USER 10001:10001

CMD ["python", "main.py"]
