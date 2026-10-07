# Two-node placement and vitals calibration

This protocol turns ESP32 placement into measured evidence instead of a visual guess. It is intentionally read-only and does **not** establish medical accuracy.

## Geometry first

Record the Wi-Fi transmitter/AP and each ESP32 receiver in one room-local metric frame. Do not enter guessed coordinates merely to make the geometry gate pass. With the current independent-clock ESP32 receivers, surveyed geometry is useful for comparing placements and multi-link diversity, but it is **not** calibrated TDOA/AoA triangulation. `scripts/ruview_placement_vitals.py geometry ...` therefore keeps `metric_triangulation_ready=false` for the current topology.

For two receivers, prefer placements where the AP→receiver paths cross the main sensing volume from materially different bearings. A same-wall/same-bearing arrangement is a useful control but normally provides less independent information. Avoid having both links blocked by the same large metal appliance, display, PC chassis, or dense furniture.

## Placement experiment

For each candidate placement, keep the room, AP, firmware, channel, and subject protocol fixed. Capture at least these phases, each with an explicit label:

1. **Empty room** — 2 minutes.
2. **Still, frontal** — 5 minutes seated or lying quietly.
3. **Still, 90° orientation** — 5 minutes.
4. **Still, 180° orientation** — 5 minutes.
5. **Ordinary walking** — 3 minutes across the sensing volume.
6. **Sit/stand transitions** — 10 repetitions.

Example (run on the sensing host; token never enters the output):

```bash
python3 scripts/ruview_placement_vitals.py capture \
  --token-file /etc/kipnerter/ruview-events.env \
  --output evidence/placement-B-frontal.jsonl \
  --duration 300 --label placement-B-frontal
python3 scripts/ruview_placement_vitals.py summary evidence/placement-B-frontal.jsonl
```

Compare placements on: two-node freshness coverage, per-node RSSI distribution and balance, classification confidence, vital estimate coverage, vital confidence, and signal quality. Do not optimize RSSI alone: two very strong but geometrically redundant links can be less informative than two moderately strong links that observe the torso from different paths.

## Vitals reference protocol

Use an independent reference whose timestamps can be exported. The CSV contract is:

```text
timestamp,heart_rate_bpm,breathing_rate_bpm
2026-10-06T20:00:00-04:00,72,14
...
```

For heart-rate evaluation, exclude ordinary walking from the primary accuracy claim. Cardiac displacement is much weaker than respiration and the current implementation explicitly needs a stable multi-subcarrier window. Report still and moving results separately rather than hiding motion dropouts in one average.

Run:

```bash
python3 scripts/ruview_placement_vitals.py agreement \
  evidence/placement-B-frontal.jsonl evidence/reference.csv
```

Minimum reporting set: MAE, RMSE, signed bias, estimate coverage, paired coverage, motion/orientation stratum, node freshness, and RuView confidence/signal quality. A low error computed from a tiny number of surviving estimates is not a pass.

Treat any of these as **insufficient signal**, not a numeric vital:

- RuView abstains / no estimate;
- fewer than the required fresh sensing nodes for that experiment;
- low signal-quality/confidence threshold selected by the validation campaign;
- multiple occupants when the calibrated-vitals contract requires one;
- excessive motion for the still-vitals claim;
- stale or poorly time-aligned reference data.

No threshold in this protocol authorizes diagnosis, treatment, or emergency medical interpretation.

## Pose experiment

Pose should be evaluated separately against independent ground truth. The current live field/skeleton view contains real CSI-driven motion but is not itself evidence of joint accuracy or metric localization. Compare one-node and two-node conditions, orientation, static/moving states, and confidence on a held-out reference sequence. Only measured ground-truth agreement should justify a stronger pose claim.