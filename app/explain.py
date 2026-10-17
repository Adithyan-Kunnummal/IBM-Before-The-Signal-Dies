"""Plain-language locality explanations built directly from the data."""

CAVEAT = "The band is a relative ranking among WA localities, not an outage probability or forecast."


def _pct(p):
    # Round down so a top locality reads "higher than 99%", never "higher than 100%".
    return f"{min(99, int(p * 100))}%"


def explanation(loc, total):
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
    parts.append(CAVEAT)
    return " ".join(parts)
