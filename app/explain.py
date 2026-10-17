"""Locality explanations: Granite when a server is configured, otherwise a data template.

Granite is reached through an OpenAI-compatible chat endpoint, which is what the
llama.cpp server exposes (for example `llama-server -m granite.gguf --port 8080`).
Set GRANITE_URL (e.g. http://127.0.0.1:8080) and optionally GRANITE_MODEL.
"""
import json
import os
import urllib.error
import urllib.request

GRANITE_URL = os.environ.get("GRANITE_URL", "").rstrip("/")
GRANITE_MODEL = os.environ.get("GRANITE_MODEL", "granite")
GRANITE_TIMEOUT_S = float(os.environ.get("GRANITE_TIMEOUT_S", "90"))

INSTRUCTIONS = (
    "You explain a relative mobile-outage susceptibility band for a Western Australian locality. "
    "Use only the JSON evidence provided. Do not change the band, rank or any number. "
    "Do not describe the band as a probability or prediction. Explain the main factors in plain "
    "language for an emergency planner, mention at least one caveat, and keep it under 120 words."
)

CAVEATS = [
    "The band is a relative ranking among WA localities, not an outage probability or forecast.",
    "Infrastructure is the January 2025 ACCC Optus site snapshot; site identifiers are not necessarily unique towers.",
    "Distances are measured from one representative point inside the locality, not from every address.",
    "Flood values of 0 can mean the area is unmapped, not flood-free.",
    "Recorded bushfire history is incomplete statewide; 0 does not prove there were no fires.",
]

_cache = {}


def granite_status():
    return {"connected": bool(GRANITE_URL), "url": GRANITE_URL, "model": GRANITE_MODEL}


def context(loc, total):
    return {
        "locality": loc["name"],
        "population_2021": loc["population"],
        "band": loc["index_band"],
        "rank": f"{loc['index_rank']} of {total} (1 = most susceptible)",
        "hazard_percentile": loc["hazard_percentile"],
        "infrastructure_sparsity_percentile": loc["infrastructure_percentile"],
        "band_stable_under_40_60_weighting": loc["band_stable"],
        "main_factors": loc["top_factors"],
        "measured": {
            "bushfire_prone_area_pct": loc["bushfire_prone_pct"],
            "mapped_floodplain_pct": loc["flood_mapped_pct"],
            "recorded_bushfires_per_year": loc["bushfires_per_year"],
            "cyclones_tracked_within_100km": loc["cyclone_tracks"],
            "nearest_optus_site_km": loc["nearest_site_km"],
            "optus_sites_within_5km": loc["sites_within_5km"],
            "optus_sites_within_10km": loc["sites_within_10km"],
        },
        "reported_outage_model": {
            "band": loc["model_band"],
            "rank": f"{loc['model_rank']} of {total}",
            "note": "Ranks where significant outages tend to be reported; it is not susceptibility.",
        },
        "appeared_in_outage_register_dec2025_oct2026": loc["reported_outage"],
        "caveats": CAVEATS,
    }


def _pct(p):
    # Round down so a top locality reads "higher than 99%", never "higher than 100%".
    return f"{min(99, int(p * 100))}%"


def template_explanation(loc, total):
    factors = loc["top_factors"]
    parts = [
        f"{loc['name']} is in the {loc['index_band']} band for relative outage susceptibility "
        f"(rank {loc['index_rank']} of {total}).",
        f"Its hazard exposure is higher than {_pct(loc['hazard_percentile'])} of WA localities and its "
        f"infrastructure sparsity is higher than {_pct(loc['infrastructure_percentile'])}.",
    ]
    if factors and not factors.startswith("no feature"):
        parts.append(f"Main factors: {factors}.")
    else:
        parts.append("No single factor sits in the worse half of the WA scale.")
    if loc["reported_outage"]:
        parts.append("It appeared in the Optus significant-outage register between December 2025 and October 2026.")
    if not loc["band_stable"]:
        parts.append("Its band changes if the hazard weighting moves to 40% or 60%, so treat it as borderline.")
    parts.append(CAVEATS[0])
    return " ".join(parts)


def _ask_granite(ctx):
    body = json.dumps({
        "model": GRANITE_MODEL,
        "temperature": 0.2,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": INSTRUCTIONS},
            {"role": "user", "content": "Evidence:\n" + json.dumps(ctx, indent=2)},
        ],
    }).encode()
    req = urllib.request.Request(f"{GRANITE_URL}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=GRANITE_TIMEOUT_S) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"].strip()


def explain(loc, total):
    ctx = context(loc, total)
    if GRANITE_URL:
        if loc["sal"] in _cache:
            return {"source": "granite", "text": _cache[loc["sal"]], "context": ctx}
        try:
            text = _ask_granite(ctx)
            _cache[loc["sal"]] = text
            return {"source": "granite", "text": text, "context": ctx}
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            return {"source": "template", "text": template_explanation(loc, total), "context": ctx,
                    "note": f"Granite request failed ({exc.__class__.__name__}); showing the data template."}
    return {"source": "template", "text": template_explanation(loc, total), "context": ctx,
            "note": "Granite is not connected; this explanation is built directly from the data."}
