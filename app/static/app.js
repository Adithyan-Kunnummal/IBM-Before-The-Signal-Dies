/* Before the Signal Dies: map, scenario controls and locality details. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const fmt = new Intl.NumberFormat("en-AU");
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const state = {
    mode: "index",            // "index" | "model"
    hazard: null,             // {lat, lon, radius_km, failure_share}
    presetId: null,
    manual: new Set(),        // site ids switched off by hand
    coverageKm: 10,
    units: 2,
    unitRadiusKm: 15,
    placing: false,
    selected: null,
  };

  let summary, localities, sites, map, renderer;
  const locMarkers = [];
  const siteMarkers = new Map();
  let overlay, hazardLayer, unitLayer, siteLayer;
  let lastResult = null;
  let runTimer = null;
  let runSeq = 0;

  // ---------- helpers ----------
  async function getJSON(url, options) {
    const res = await fetch(url, options);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || `Request failed (${res.status})`);
    return body;
  }
  const bandColor = (band) => css(band === "High" ? "--high" : band === "Medium" ? "--medium" : "--low");
  const markerRadius = (pop) => 2.5 + Math.log10(pop + 1) * 1.5;
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // ---------- map ----------
  function buildMap() {
    renderer = L.canvas({ padding: 0.3 });
    map = L.map("map", { preferCanvas: true, zoomSnap: 0.25, minZoom: 4 });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 15, opacity: 0.55,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    map.fitBounds([[-35.3, 112.8], [-13.6, 129.1]]);

    const locLayer = L.layerGroup().addTo(map);
    localities.forEach((loc, i) => {
      const m = L.circleMarker([loc.lat, loc.lon], {
        renderer, radius: markerRadius(loc.population), weight: 0.6, color: css("--panel"),
        fillOpacity: 0.88,
      });
      m.bindTooltip(() => {
        const raised = state.mode === "index" && raisedBands.has(loc.sal);
        const band = state.mode === "index" ? currentIndexBand(loc) : loc.model_band;
        return `<strong>${escapeHtml(loc.name)}</strong><br>${fmt.format(loc.population)} residents · ${band} band` +
          (raised ? ` (was ${loc.index_band} before the disaster)` : "");
      }, { sticky: true });
      m.on("click", () => selectLocality(loc.sal));
      m.addTo(locLayer);
      locMarkers[i] = m;
    });
    colourLocalities();

    siteLayer = L.layerGroup().addTo(map);
    sites.forEach((s) => {
      const m = L.circleMarker([s.lat, s.lon], {
        renderer, radius: 2.2, weight: 0, fillColor: css("--site"), fillOpacity: 0.9,
      });
      m.bindTooltip(() => `Optus site ${escapeHtml(s.id)}${s.co_funded ? " (co-funded)" : ""}<br>` +
        `${isFailed(s.id) ? "Down in this scenario" : "Click to switch off"}`);
      m.on("click", (e) => { L.DomEvent.stop(e); toggleSite(s.id); });
      m.addTo(siteLayer);
      siteMarkers.set(s.id, m);
    });

    overlay = L.layerGroup().addTo(map);
    hazardLayer = L.layerGroup().addTo(map);
    unitLayer = L.layerGroup().addTo(map);

    map.on("click", (e) => {
      if (!state.placing) return;
      const base = state.hazard || { radius_km: +$("radius").value, failure_share: +$("share").value };
      state.hazard = { ...base, lat: e.latlng.lat, lon: e.latlng.lng };
      setPreset(null);
      setPlacing(false);
      scheduleRun();
    });
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // sal -> band after the disaster, for localities whose susceptibility band rose.
  let raisedBands = new Map();
  const currentIndexBand = (loc) => raisedBands.get(loc.sal) || loc.index_band;

  function colourLocalities() {
    localities.forEach((loc, i) => {
      const band = state.mode === "index" ? currentIndexBand(loc) : loc.model_band;
      locMarkers[i].setStyle({ fillColor: bandColor(band) });
    });
  }

  const failedSet = () => new Set(lastResult ? lastResult.failed_site_ids : []);
  const isFailed = (id) => failedSet().has(id) || state.manual.has(id);

  // ---------- controls ----------
  function bindControls() {
    $("mode-index").addEventListener("click", () => setMode("index"));
    $("mode-model").addEventListener("click", () => setMode("model"));
    $("show-sites").addEventListener("change", (e) => {
      if (e.target.checked) siteLayer.addTo(map); else siteLayer.remove();
    });
    $("place-area").addEventListener("click", () => setPlacing(!state.placing));

    const slider = (id, out, suffix, apply) => {
      $(id).addEventListener("input", (e) => {
        const v = +e.target.value;
        $(out).textContent = `${v}${suffix}`;
        apply(v);
        scheduleRun();
      });
    };
    slider("radius", "radius-out", " km", (v) => { if (state.hazard) state.hazard.radius_km = v; setPreset(null, true); });
    slider("share", "share-out", "%", (v) => { if (state.hazard) state.hazard.failure_share = v; setPreset(null, true); });
    slider("coverage", "coverage-out", " km", (v) => { state.coverageKm = v; });
    slider("units", "units-out", "", (v) => { state.units = v; });
    slider("unit-radius", "unit-radius-out", " km", (v) => { state.unitRadiusKm = v; });

    $("d-close").addEventListener("click", () => { $("detail").hidden = true; state.selected = null; });

    const presets = $("presets");
    summary.presets.forEach((p) => {
      const b = el("button", "preset", p.name);
      b.type = "button";
      b.dataset.id = p.id;
      b.addEventListener("click", () => applyPreset(p));
      presets.appendChild(b);
    });
    const clear = el("button", "preset", "No disaster");
    clear.type = "button";
    clear.dataset.id = "none";
    clear.addEventListener("click", () => { state.hazard = null; state.manual.clear(); setPreset("none"); scheduleRun(); });
    presets.appendChild(clear);
  }

  function setMode(mode) {
    state.mode = mode;
    $("mode-index").classList.toggle("is-on", mode === "index");
    $("mode-model").classList.toggle("is-on", mode === "model");
    $("mode-index").setAttribute("aria-checked", String(mode === "index"));
    $("mode-model").setAttribute("aria-checked", String(mode === "model"));
    $("mode-hint").textContent = mode === "index"
      ? "Hazard exposure and sparse Optus infrastructure, weighted 50/50. Relative bands, not probabilities."
      : "Where significant Optus outages tend to be reported (XGBoost, trained on the outage register). Mostly near Optus sites, so it misses communities with little network.";
    colourLocalities();
  }

  function setPlacing(on) {
    state.placing = on;
    $("place-area").classList.toggle("is-on", on);
    $("place-area").textContent = on ? "Click the map now… (cancel)" : "Click the map to move the area";
    $("map-note").hidden = !on;
    map.getContainer().style.cursor = on ? "crosshair" : "";
  }

  function setPreset(id, keepIfNull) {
    if (id === null && keepIfNull && !state.presetId) return;
    state.presetId = id;
    document.querySelectorAll(".preset").forEach((b) => b.classList.toggle("is-on", b.dataset.id === id));
  }

  function applyPreset(p) {
    state.hazard = { lat: p.lat, lon: p.lon, radius_km: p.radius_km, failure_share: p.failure_share };
    state.manual.clear();
    $("radius").value = p.radius_km; $("radius-out").textContent = `${p.radius_km} km`;
    $("share").value = p.failure_share; $("share-out").textContent = `${p.failure_share}%`;
    setPreset(p.id);
    const circle = L.circle([p.lat, p.lon], { radius: p.radius_km * 1000 });
    circle.addTo(map);
    map.fitBounds(circle.getBounds(), { padding: [40, 40], maxZoom: 10 });
    circle.remove();
    scheduleRun();
  }

  function toggleSite(id) {
    if (state.manual.has(id)) state.manual.delete(id); else state.manual.add(id);
    scheduleRun();
  }

  // ---------- simulation ----------
  function scheduleRun() {
    clearTimeout(runTimer);
    runTimer = setTimeout(run, 120);
  }

  async function run() {
    const seq = ++runSeq;
    const body = {
      hazard: state.hazard,
      manual_site_ids: [...state.manual],
      coverage_km: state.coverageKm,
      units: state.units,
      unit_radius_km: state.unitRadiusKm,
    };
    try {
      const result = await getJSON("api/simulate", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (seq !== runSeq) return;
      lastResult = result;
      render(result);
    } catch (err) {
      $("compute-note").textContent = err.message;
    }
  }

  function render(r) {
    // Bands after the disaster are needed by every list below, so set them first.
    raisedBands = new Map(r.band_changes.list.map((c) => [c.sal, c.band_after]));
    const bandNow = (sal, before) => raisedBands.get(sal) || before;

    // sites
    const failed = new Set(r.failed_site_ids);
    siteMarkers.forEach((m, id) => {
      const down = failed.has(id);
      m.setStyle({ fillColor: down ? css("--lost") : css("--site"), fillOpacity: down ? 1 : 0.9 });
      m.setRadius(down ? 4 : 2.2);
    });
    $("manual-count").textContent = state.manual.size ? `${state.manual.size} switched off by hand.` : "";

    // hazard
    hazardLayer.clearLayers();
    if (state.hazard) {
      L.circle([state.hazard.lat, state.hazard.lon], {
        radius: state.hazard.radius_km * 1000, color: css("--lost"), weight: 1.5, dashArray: "6 6",
        fillColor: css("--lost"), fillOpacity: 0.06, interactive: false,
      }).addTo(hazardLayer);
    }

    // lost / restored rings
    overlay.clearLayers();
    const bySal = new Map(localities.map((l) => [l.sal, l]));
    r.lost_coverage.list.forEach((item) => {
      const loc = bySal.get(item.sal);
      L.circleMarker([loc.lat, loc.lon], {
        renderer, radius: markerRadius(loc.population) + 3.5, weight: 2.2, fill: false,
        color: item.restored ? css("--restored") : css("--lost"), interactive: false,
      }).addTo(overlay);
    });

    // units
    unitLayer.clearLayers();
    r.units.forEach((u, i) => {
      L.circle([u.lat, u.lon], {
        radius: r.assumptions.unit_radius_km * 1000, color: css("--restored"), weight: 1.5,
        dashArray: "3 5", fillColor: css("--restored"), fillOpacity: 0.07, interactive: false,
      }).addTo(unitLayer);
      L.marker([u.lat, u.lon], {
        icon: L.divIcon({ className: "", html: `<div class="unit-icon">${i + 1}</div>`, iconSize: [22, 22] }),
        keyboard: false,
      }).bindTooltip(`Unit ${i + 1} at ${escapeHtml(u.at)}: restores ${fmt.format(u.restored_residents)} residents`)
        .addTo(unitLayer);
    });

    // tiles
    $("t-sites").textContent = `${fmt.format(r.sites_failed)}`;
    $("t-lost").textContent = fmt.format(r.lost_coverage.localities);
    $("t-res").textContent = fmt.format(r.lost_coverage.residents);
    $("t-restored").textContent = fmt.format(r.restored.residents);
    const machine = summary.server_machine || "server";
    $("compute-note").textContent = `${machine} · ${r.compute_ms} ms`;

    // unit list
    const ul = $("unit-list");
    ul.replaceChildren();
    r.units.forEach((u, i) => {
      const li = el("li");
      li.appendChild(el("span", "unit-no", `Unit ${i + 1}`));
      li.appendChild(el("span", "", `Place at ${u.at}: restores ${fmt.format(u.restored_residents)} residents`));
      li.appendChild(el("span", "unit-meta", `${u.restored_localities} localities: ${u.restored_names.join(", ")}${u.restored_localities > u.restored_names.length ? "…" : ""}`));
      ul.appendChild(li);
    });

    // lost list
    const lost = $("lost-list");
    lost.replaceChildren();
    if (!r.lost_coverage.list.length) {
      lost.appendChild(el("li", "empty", state.hazard || state.manual.size
        ? "No community loses coverage under these assumptions."
        : "Choose a scenario or switch off sites to see who loses coverage."));
    }
    r.lost_coverage.list.slice(0, 12).forEach((item) => {
      const li = el("li");
      const b = el("button", "", item.name);
      b.type = "button";
      b.addEventListener("click", () => selectLocality(item.sal, true));
      li.appendChild(b);
      const now = bandNow(item.sal, item.index_band);
      li.appendChild(el("span", `chip chip-band band-${now}`, now));
      li.appendChild(el("span", item.restored ? "tag-restored" : "pop",
        item.restored ? "restored" : fmt.format(item.population)));
      lost.appendChild(li);
    });
    if (r.lost_coverage.list.length > 12) {
      lost.appendChild(el("li", "empty", `and ${r.lost_coverage.list.length - 12} more`));
    }
    // susceptibility re-banded live
    colourLocalities();
    $("band-head").textContent = r.band_changes.list.length
      ? `Susceptibility raised by the disaster: ${r.band_changes.list.length} localities, ${r.band_changes.to_high} now High`
      : "Susceptibility raised by the disaster";
    const bands = $("band-list");
    bands.replaceChildren();
    if (!r.band_changes.list.length) {
      bands.appendChild(el("li", "empty", "No locality changes band."));
    }
    r.band_changes.list.slice(0, 8).forEach((c) => {
      const li = el("li");
      const b = el("button", "", c.name);
      b.type = "button";
      b.addEventListener("click", () => selectLocality(c.sal, true));
      li.appendChild(b);
      li.appendChild(el("span", "band-shift", `${c.band_before} → ${c.band_after}`));
      li.appendChild(el("span", "pop", `${c.nearest_site_km_before} → ${c.nearest_site_km_after} km`));
      bands.appendChild(li);
    });
    if (r.band_changes.list.length > 8) {
      bands.appendChild(el("li", "empty", `and ${r.band_changes.list.length - 8} more`));
    }

    const base = r.baseline_without_coverage;
    $("baseline-note").textContent =
      `Before any disaster, ${fmt.format(base.localities)} localities (${fmt.format(base.residents)} residents) have no Optus site within ${r.assumptions.coverage_km} km of their centre point.` +
      (r.lost_coverage.list.length
        ? ` After the disaster, ${r.lost_coverage.list.filter((i) => bandNow(i.sal, i.index_band) === "High").length} of the ${r.lost_coverage.list.length} communities losing coverage are in the High susceptibility band.`
        : "");
  }

  // ---------- locality detail ----------
  async function selectLocality(sal, pan) {
    state.selected = sal;
    const panel = $("detail");
    panel.hidden = false;
    $("d-text").textContent = "Loading…";
    try {
      const { locality: l, explanation: x, live_model: live } = await getJSON(`api/locality/${encodeURIComponent(sal)}`);
      if (state.selected !== sal) return;
      if (pan) map.setView([l.lat, l.lon], Math.max(map.getZoom(), 9));
      $("d-name").textContent = l.name;
      const chips = $("d-chips");
      chips.replaceChildren(
        el("span", `chip chip-band band-${l.index_band}`, `Index: ${l.index_band}`),
        el("span", `chip chip-band band-${l.model_band}`, `Model: ${l.model_band}`),
      );
      if (raisedBands.has(l.sal)) {
        chips.appendChild(el("span", `chip chip-band band-${raisedBands.get(l.sal)}`, `After disaster: ${raisedBands.get(l.sal)}`));
      }
      if (l.reported_outage) chips.appendChild(el("span", "chip chip-warn", "In outage register"));
      if (!l.band_stable) chips.appendChild(el("span", "chip", "Borderline band"));

      const facts = [
        ["Residents (2021)", fmt.format(l.population)],
        ["Index rank", `${l.index_rank} of ${localities.length}`],
        ["Model rank", `${l.model_rank} of ${localities.length}`],
        ["Nearest Optus site", `${l.nearest_site_km} km`],
        ["Optus sites within 10 km", l.sites_within_10km],
        ["Bushfire-prone area", `${l.bushfire_prone_pct}%`],
        ["Mapped floodplain", l.flood_mapping_status === "unknown_no_overlap" ? "none mapped" : `${l.flood_mapped_pct}%`],
        ["Recorded bushfires / year", l.bushfires_per_year],
        ["Cyclones within 100 km", l.cyclone_tracks],
      ];
      const dl = $("d-facts");
      dl.replaceChildren();
      facts.forEach(([k, v]) => { dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", String(v))); });

      $("d-text").textContent = x;
      renderLiveModel(live);
      panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    } catch (err) {
      $("d-text").textContent = err.message;
    }
  }

  const backendLabel = (name) => ((summary.model_service.backends || {})[name] || {}).label || name;
  const featureLabel = (f) => f.replace(/_2025|_2021|_before_window/g, "").replace(/_/g, " ");

  function renderLiveModel(live) {
    const box = $("d-live");
    box.hidden = !live;
    if (!live) return;
    const scores = $("d-live-scores");
    scores.replaceChildren();
    Object.entries(live.scores).forEach(([name, s]) => {
      const span = el("span");
      span.appendChild(el("strong", "", s.score.toFixed(3)));
      span.appendChild(document.createTextNode(` ${backendLabel(name)} · ${s.ms} ms`));
      scores.appendChild(span);
    });
    const list = $("d-live-contrib");
    list.replaceChildren();
    live.contributions.forEach((c) => {
      const li = el("li");
      li.appendChild(el("span", "", `${featureLabel(c.feature)} = ${c.value}`));
      li.appendChild(el("span", c.contribution >= 0 ? "up" : "down",
        `${c.contribution >= 0 ? "+" : ""}${c.contribution}`));
      list.appendChild(li);
    });
  }

  function renderPlatform() {
    const ms = summary.model_service || { backends: {}, cpu: {}, errors: [] };
    const cpu = ms.cpu || {};
    $("platform-machine").textContent = summary.server_machine;
    const facts = [
      ["Processor architecture", cpu.machine || summary.server_machine],
      ["Machine type", cpu.machine_type || "not reported"],
      ["On-chip AI accelerator (NNPA)", cpu.nnpa ? "available" : "not detected"],
      ["Python", summary.server_python],
    ];
    Object.entries(ms.backends).forEach(([name, info]) => {
      facts.push([`Model via ${info.label || name}`,
        `${info.accelerator}${info.tree_format ? ` (${info.tree_format})` : ""}`]);
    });
    const dl = $("platform-facts");
    dl.replaceChildren();
    facts.forEach(([k, v]) => { dl.appendChild(el("dt", "", k)); dl.appendChild(el("dd", "", String(v))); });
    $("platform-note").textContent = ms.errors.length ? ms.errors.join(" ") :
      "Every model library was checked against the scores saved from the LinuxONE training run.";
    $("run-bench").hidden = !Object.keys(ms.backends).length;

    const chip = $("status-accel");
    const accelInUse = Object.values(ms.backends).some((b) => /Telum/.test(b.accelerator || ""));
    if (accelInUse || cpu.nnpa) {
      chip.hidden = false;
      chip.textContent = accelInUse ? "Telum AI accelerator in use" : "Telum AI accelerator available";
      chip.className = `chip ${accelInUse ? "chip-ok" : "chip-muted"}`;
    }
  }

  async function runBenchmark() {
    const btn = $("run-bench");
    btn.disabled = true;
    btn.textContent = "Running…";
    try {
      const b = await getJSON("api/model/benchmark");
      const rows = $("bench-rows");
      rows.replaceChildren();
      const base = (b.results.xgboost || {}).median_ms;
      Object.entries(b.results).forEach(([name, r]) => {
        const tr = el("tr");
        tr.appendChild(el("td", "", backendLabel(name)));
        tr.appendChild(el("td", "", ((b.backends[name] || {}).accelerator) || "CPU"));
        tr.appendChild(el("td", "num", `${r.median_ms} ms`));
        tr.appendChild(el("td", "num", `${r.per_locality_us} µs`));
        tr.appendChild(el("td", "num", base ? `${(base / r.median_ms).toFixed(1)}×` : "–"));
        rows.appendChild(tr);
      });
      $("bench").hidden = false;
    } catch (err) {
      $("platform-note").textContent = err.message;
    } finally {
      btn.disabled = false;
      btn.textContent = "Run inference benchmark again";
    }
  }

  // ---------- start ----------
  async function start() {
    try {
      [summary, localities, sites] = await Promise.all([
        getJSON("api/summary"), getJSON("api/localities"), getJSON("api/sites"),
      ]);
    } catch (err) {
      $("status-server").textContent = `Server not reachable: ${err.message}`;
      return;
    }
    const srv = $("status-server");
    srv.textContent = `Running on ${summary.server_machine}${summary.server_machine === "s390x" ? " (IBM LinuxONE)" : ""}`;
    srv.className = `chip ${summary.server_machine === "s390x" ? "chip-ok" : "chip-muted"}`;

    const xgb = summary.model_test && summary.model_test.XGBoost;
    if (xgb) {
      $("model-test").textContent =
        `On held-out incidents it placed ${Math.round(xgb.captured_top_20pct * 100)}% of reported localities in its top 20% (random: 20%).`;
    }

    renderPlatform();
    $("run-bench").addEventListener("click", runBenchmark);
    buildMap();
    bindControls();
    applyPreset(summary.presets[0]);
  }

  start();
})();
