# Before the Signal Dies: prototype app container.
# Ubuntu 24.04 matches the LinuxONE host and packages IBM's zDNN library (libzdnn0), which
# Snap ML needs to run inference on the Telum AI accelerator. NNPA is a CPU instruction,
# so the container needs no device access or extra privileges to use it.
#
#   docker build -t signal-dies .
#   docker run -d --name signal-dies -p 127.0.0.1:8050:8050 \
#     --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
#     --memory 1g --pids-limit 256 signal-dies

# ---- build stage: compilers only live here ----
# scikit-learn and SciPy (needed by Snap ML) have no s390x wheels, so they compile here.
# Expect the first build to take 20-40 minutes; later builds reuse the cached layer.
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-dev python3-venv \
      build-essential cmake gfortran pkg-config libopenblas-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/venv
COPY app/requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip wheel --no-cache-dir --wheel-dir /wheels -r /tmp/requirements.txt

# ---- runtime stage ----
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH
# Runtime libraries for compiled packages (SciPy needs the Fortran runtime), plus zDNN on s390x.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv libgomp1 libopenblas0 libgfortran5 \
 && if [ "$(dpkg --print-architecture)" = "s390x" ]; then apt-get install -y --no-install-recommends libzdnn0; fi \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
COPY --from=build /wheels /wheels
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --no-index --find-links /wheels /wheels/*.whl \
 && rm -rf /wheels

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
