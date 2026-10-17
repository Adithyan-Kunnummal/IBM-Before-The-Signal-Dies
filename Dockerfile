# Before the Signal Dies: prototype app container.
# The python base image is multi-architecture, so this builds natively on LinuxONE (s390x).
#
#   docker build -t signal-dies .
#   docker run -d --name signal-dies -p 127.0.0.1:8050:8050 \
#     --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
#     --memory 1g --pids-limit 256 signal-dies

# ---- build stage: compilers only live here ----
# scikit-learn and SciPy (needed by Snap ML) have no s390x wheels, so they compile here.
# Expect the first build to take 20-40 minutes; later builds reuse the cached layer.
FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake gfortran pkg-config libopenblas-dev \
 && rm -rf /var/lib/apt/lists/*
COPY app/requirements.txt /tmp/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Runtime libraries for packages compiled in the build stage (SciPy needs the Fortran runtime).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libopenblas0 libgfortran5 \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links /wheels /wheels/*.whl && rm -rf /wheels

WORKDIR /srv
COPY --chown=root:root app/ app/
COPY --chown=root:root models/xgb_pu_outage.json models/
COPY --chown=root:root data/processed/wa_susceptibility_index.csv \
     data/processed/wa_susceptibility_index.metadata.json \
     data/processed/wa_outage_pu_scores.csv \
     data/processed/wa_outage_pu_model.metadata.json \
     data/processed/wa_outage_modelling.csv \
     data/processed/outage_data_quality.json \
     data/processed/
COPY --chown=root:root data/raw/accc/optus_sites_wa_envelope_2025-01-31.geojson data/raw/accc/

# Code and data are owned by root and read-only to the app user.
USER 10001
EXPOSE 8050
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8050/api/health', timeout=4)"]
CMD ["python", "app/server.py", "--host", "0.0.0.0", "--port", "8050"]
