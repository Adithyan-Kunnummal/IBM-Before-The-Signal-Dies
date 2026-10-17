# Before the Signal Dies: prototype app

An interactive map for planning disaster communications in Western Australia:

- Colours every locality by the **susceptibility index** or by the **reported-outage model** (XGBoost).
- Runs a **disaster scenario**: Optus sites inside an area fail, and the map shows which communities lose coverage and how many residents are affected.
- Places **temporary communications units** where they restore coverage to the most residents.
- Explains any locality with **IBM Granite** when a Granite server is running, or with a data template otherwise.

The server uses the Python standard library only, so it runs on LinuxONE without installing anything. The map library (Leaflet 1.9.4) is bundled in `static/vendor`. Map tiles come from OpenStreetMap and need internet access in the viewer's browser; without it, the localities and sites still draw on a plain background.

## Run it

From the repository root (Python 3.9 or newer):

```bash
python3 app/server.py                 # http://127.0.0.1:8050
python3 app/server.py --port 9000     # another port
python3 app/server.py --host 0.0.0.0  # accept connections from other machines
```

It reads `data/processed/wa_susceptibility_index.csv`, `data/processed/wa_outage_pu_scores.csv`, their metadata, and `data/raw/accc/optus_sites_wa_envelope_2025-01-31.geojson`, all of which are in the repository.

## Open it from your laptop when it runs on LinuxONE

**SSH virtual machine (recommended).** Keep the server on `127.0.0.1` and tunnel the port. On your laptop:

```bash
ssh -i path/to/key -L 8050:127.0.0.1:8050 <user>@<linuxone-ip>
```

Then start the server in that SSH session and open http://127.0.0.1:8050 in your browser. No firewall change is needed.

**JupyterLab only.** Start the server from a JupyterLab terminal. If the lab has `jupyter-server-proxy`, open `<your lab URL>/proxy/8050/`, including the trailing slash. If it does not, ask the event organisers how to expose a port.

## Connect Granite

The app talks to any OpenAI-compatible chat endpoint, which is what the llama.cpp server provides. For example, with a Granite GGUF model on LinuxONE:

```bash
llama-server -m granite-3.3-2b-instruct-Q4_K_M.gguf --port 8080 &
GRANITE_URL=http://127.0.0.1:8080 python3 app/server.py
```

| Variable | Default | Meaning |
|---|---|---|
| `GRANITE_URL` | unset | Base URL of the chat server. Unset means template explanations. |
| `GRANITE_MODEL` | `granite` | Model name sent in the request. |
| `GRANITE_TIMEOUT_S` | `90` | Seconds to wait for an answer before falling back to the template. |

Granite receives only the selected locality's band, rank, measured facts and caveats, and is instructed not to change any band or number. If a request fails, the app shows the data template and says so.

## Assumptions to state in the demo

- The ACCC data has no per-site footprints. A locality counts as **covered** when a working Optus site is within the assumed coverage radius (slider, default 10 km) of its representative point.
- The preset scenarios are illustrative, not reconstructions of real events. "Sites in the area that fail" picks a fixed subset of sites, so results repeat.
- A temporary unit restores localities within its range (slider, default 15 km). Units are placed one at a time where each restores the most residents not already restored.
- Index bands are relative rankings, not outage probabilities. The reported-outage model shows where significant outages get reported, mostly near Optus sites.

## API

| Method | Path | Returns |
|---|---|---|
| GET | `/api/summary` | Counts, preset scenarios, model test results, server machine, Granite status |
| GET | `/api/localities` | Locality points with both bands |
| GET | `/api/sites` | Optus site positions |
| GET | `/api/locality/<SAL code>` | Full locality facts and an explanation |
| POST | `/api/simulate` | Body: `hazard` (`lat`, `lon`, `radius_km`, `failure_share`), `manual_site_ids`, `coverage_km`, `units`, `unit_radius_km` |

## Tests

```bash
python3 -m unittest discover -s app -p "test_*.py"
```
