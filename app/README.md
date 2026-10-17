# Before the Signal Dies: prototype app

An interactive map for planning disaster communications in Western Australia:

- Colours every locality by the **susceptibility index** or by the **reported-outage model** (XGBoost).
- Runs a **disaster scenario**: Optus sites inside an area fail, and the map shows which communities lose coverage and how many residents are affected.
- **Re-bands susceptibility live**: localities that lose nearby sites are recalculated with the index's own scaling, and the map shows which ones move up a band.
- Places **temporary communications units** where they restore coverage to the most residents.
- **Scores the trained model live on LinuxONE** with XGBoost and IBM Snap ML, on the CPU cores and on the Telum on-chip AI accelerator.
- Explains any locality in plain language, built directly from its measured facts.

The simulation, re-banding and web server use the Python standard library only. Live model inference needs `numpy`, `xgboost` and `snapml` (see `requirements.txt`); without them the app still runs and says which library is missing. The map library (Leaflet 1.9.4) is bundled in `static/vendor`. Map tiles come from OpenStreetMap and need internet access in the viewer's browser; without it, the localities and sites still draw on a plain background.

## Run it

From the repository root (Python 3.9 or newer):

```bash
pip install -r app/requirements.txt    # optional: live model inference
python3 app/server.py                  # http://127.0.0.1:8050
python3 app/server.py --port 9000      # another port
python3 app/server.py --host 0.0.0.0   # accept connections from other machines
```

At start-up the server prints the processor, whether the AI accelerator (`nnpa`) is present, and which model libraries loaded and on what hardware.

## Live model inference and Telum

`model_service.py` loads `models/xgb_pu_outage.json` (trained on LinuxONE) with both libraries:

- **XGBoost** on the CPU, also used for each locality's feature contributions.
- **IBM Snap ML on the CPU** (`compress_trees` tree format).
- **IBM Snap ML on Telum** (`zdnn_tensors` tree format), which runs the trees on the Integrated Accelerator for AI. It is loaded only when the processor reports `nnpa`; it also needs IBM's zDNN library (`sudo apt install libzdnn0` on Ubuntu; the container installs it).

Snap ML 1.17 cannot read the XGBoost 3 JSON layout ("no objective defined"), so the app writes a temporary copy in the XGBoost 2 layout. Only the layout changes, not the trees. Every engine's predictions are checked against the stored scores at start-up. The "Running on LinuxONE" panel shows which engines loaded and where, and benchmarks them.

Measured on LinuxONE (z17), median ms per batch:

| Rows | Snap ML on Telum | Snap ML on CPU | XGBoost |
|---:|---:|---:|---:|
| 1,699 | 15.1 | 1.0 | 2.2 |
| 16,990 | 66.3 | 9.8 | 20.4 |
| 169,900 | 665.9 | 99.7 | 210.8 |
| 509,700 | 1,992.1 | 299.0 | 640.6 |

Snap ML on the CPU is about twice as fast as XGBoost. For this small model (150 trees of depth 2), the accelerator is slower than the cores at every batch size tested; it takes the work off the general-purpose cores and returns the same scores (within 1.5e-7).

To check the accelerator directly:

```bash
python3 app/check_accelerator.py
```

It prints the CPU details, whether zDNN is installed, which Snap ML model layouts and tree formats load, whether they agree with XGBoost, and timings across batch sizes.

## Run it in a container

The image builds natively on LinuxONE (s390x). Compilers are only in the build stage, in case a package needs compiling; the runtime image runs as a non-root user with the code and data read-only.

```bash
docker build -t signal-dies .
docker run -d --name signal-dies -p 127.0.0.1:8050:8050 \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --memory 1g --pids-limit 256 signal-dies
docker ps            # STATUS shows "healthy" once /api/health answers
docker logs signal-dies
```

## Open it from your laptop when it runs on LinuxONE

**SSH virtual machine (recommended).** Keep the server (or the container's published port) on `127.0.0.1` and tunnel the port. On your laptop:

```bash
ssh -i path/to/key -L 8050:127.0.0.1:8050 <user>@<linuxone-ip>
```

Then open http://127.0.0.1:8050 in your browser. No firewall change is needed.

**JupyterLab only.** Start the server from a JupyterLab terminal. If the lab has `jupyter-server-proxy`, open `<your lab URL>/proxy/8050/`, including the trailing slash.

## Security

- The app is reachable only through the SSH tunnel: it listens on `127.0.0.1`, and the container publishes its port on `127.0.0.1` only.
- The container runs as an unprivileged user with a read-only file system, all Linux capabilities dropped, no privilege escalation, and memory and process limits.
- Requests are validated: body size is capped, simulation inputs are type-checked and clamped, and static file paths cannot leave the `static` folder.
- The app stores no user data and needs no credentials.

## Scalability

- Each request is independent and the data is read-only, so the app scales horizontally: run several containers behind a load balancer.
- Simulations run in a few milliseconds to about a tenth of a second; distances to nearby sites are precomputed at start-up.
- Batch model inference covers all 1,699 localities in milliseconds, so state-wide re-scoring can run on every request.

## Assumptions to state in the demo

- The ACCC data has no per-site footprints. A locality counts as **covered** when a working Optus site is within the assumed coverage radius (slider, default 10 km) of its representative point.
- The preset scenarios are illustrative, not reconstructions of real events. "Sites in the area that fail" picks a fixed subset of sites, so results repeat.
- Live re-banding changes only the infrastructure part of the index (nearest working site and working sites within 5 and 10 km). Hazard exposure is unchanged, so bands can only rise.
- A temporary unit restores localities within its range (slider, default 15 km). Units are placed one at a time where each restores the most residents not already restored.
- Index bands are relative rankings, not outage probabilities. The reported-outage model shows where significant outages get reported, mostly near Optus sites.

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/health` | Status and loaded model libraries (used by the container health check) |
| GET | `/api/summary` | Counts, preset scenarios, model test results, server machine, model and accelerator status |
| GET | `/api/localities` | Locality points with both bands |
| GET | `/api/sites` | Optus site positions |
| GET | `/api/locality/<SAL code>` | Locality facts, an explanation, and live model scores with feature contributions |
| GET | `/api/model/benchmark` | Batch inference timings per model library |
| POST | `/api/simulate` | Body: `hazard` (`lat`, `lon`, `radius_km`, `failure_share`), `manual_site_ids`, `coverage_km`, `units`, `unit_radius_km` |

## Tests

```bash
python3 -m unittest discover -s app -p "test_*.py"
```
