#!/usr/bin/env python3
"""Read-only RuView placement and vitals calibration harness.

This tool never changes RuView, node configuration, or network state. It samples
existing authenticated sensing state into a local JSONL evidence file, summarizes
multi-node signal quality, and optionally compares aggregate vital estimates with
an independent timestamped reference.

Numeric vitals are experimental engineering measurements, not medical readings.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "ruview-placement-vitals-v1"
GEOMETRY_SCHEMA = "ruview-room-geometry-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def sanitize_snapshot(payload: dict[str, Any], *, label: str = "") -> dict[str, Any]:
    """Keep only evidence needed for placement/vitals evaluation.

    Authentication material and unrelated source payload fields never enter the
    evidence file. Vital values remain local because they are needed for reference
    comparison; callers should treat the JSONL as sensitive measurement data.
    """
    nodes: list[dict[str, Any]] = []
    for raw in payload.get("node_features") or []:
        if not isinstance(raw, dict) or raw.get("node_id") is None:
            continue
        cls = raw.get("classification") if isinstance(raw.get("classification"), dict) else {}
        feat = raw.get("features") if isinstance(raw.get("features"), dict) else {}
        nodes.append({
            "node_id": raw.get("node_id"),
            "rssi_dbm": _num(raw.get("rssi_dbm")),
            "last_seen_ms": _num(raw.get("last_seen_ms")),
            "frame_rate_hz": _num(raw.get("frame_rate_hz")),
            "stale": bool(raw.get("stale", True)),
            "presence": bool(cls.get("presence", False)),
            "confidence": _num(cls.get("confidence")),
            "motion_level": str(cls.get("motion_level") or "unknown"),
            "motion_band_power": _num(feat.get("motion_band_power")),
            "breathing_band_power": _num(feat.get("breathing_band_power")),
            "variance": _num(feat.get("variance")),
        })
    vitals = payload.get("vital_signs") if isinstance(payload.get("vital_signs"), dict) else {}
    cls = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
    return {
        "schema": SCHEMA,
        "captured_at": utc_now(),
        "capture_unix_s": time.time(),
        "label": label,
        "source": str(payload.get("source") or "unknown"),
        "tick": payload.get("tick"),
        "estimated_persons": payload.get("estimated_persons"),
        "classification": {
            "presence": bool(cls.get("presence", False)),
            "confidence": _num(cls.get("confidence")),
            "motion_level": str(cls.get("motion_level") or "unknown"),
        },
        "vital_signs": {
            "heart_rate_bpm": _num(vitals.get("heart_rate_bpm")),
            "heartbeat_confidence": _num(vitals.get("heartbeat_confidence")),
            "breathing_rate_bpm": _num(vitals.get("breathing_rate_bpm")),
            "breathing_confidence": _num(vitals.get("breathing_confidence")),
            "signal_quality": _num(vitals.get("signal_quality")),
        },
        "nodes": sorted(nodes, key=lambda row: str(row["node_id"])),
    }


def fetch_json(base_url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/sensing/latest",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=4) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("sensing payload is not an object")
    return payload


def load_token(path: str | None) -> str:
    token = os.environ.get("RUVIEW_API_TOKEN", "").strip()
    if token:
        return token
    if path:
        raw = Path(path).read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.strip().startswith("RUVIEW_API_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
                break
        else:
            token = raw.strip()
    if not token:
        raise ValueError("RUVIEW_API_TOKEN or --token-file is required")
    return token


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("schema") == SCHEMA:
            rows.append(row)
    return rows


def stats(values: Iterable[float]) -> dict[str, float] | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not vals:
        return None
    return {
        "mean": statistics.fmean(vals),
        "min": min(vals),
        "max": max(vals),
        "sd": statistics.pstdev(vals),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    per_node: dict[str, dict[str, Any]] = {}
    for row in rows:
        for node in row.get("nodes") or []:
            key = str(node.get("node_id"))
            bucket = per_node.setdefault(key, {"samples": 0, "fresh": 0, "rssi": [], "frame_rate": [], "confidence": []})
            bucket["samples"] += 1
            bucket["fresh"] += int(not bool(node.get("stale", True)))
            for src, dest in (("rssi_dbm", "rssi"), ("frame_rate_hz", "frame_rate"), ("confidence", "confidence")):
                value = _num(node.get(src))
                if value is not None:
                    bucket[dest].append(value)

    node_summary = {}
    for node_id, b in sorted(per_node.items()):
        node_summary[node_id] = {
            "samples": b["samples"],
            "fresh_ratio": b["fresh"] / b["samples"] if b["samples"] else 0.0,
            "rssi_dbm": stats(b["rssi"]),
            "frame_rate_hz": stats([v for v in b["frame_rate"] if v > 0.0]),
            "classification_confidence": stats(b["confidence"]),
        }

    vital_keys = ("heart_rate_bpm", "breathing_rate_bpm", "heartbeat_confidence", "breathing_confidence", "signal_quality")
    vital_summary: dict[str, Any] = {}
    for key in vital_keys:
        values = [_num((r.get("vital_signs") or {}).get(key)) for r in rows]
        numeric = [v for v in values if v is not None]
        vital_summary[key] = {
            "coverage_ratio": len(numeric) / len(rows) if rows else 0.0,
            "stats": stats(numeric),
        }

    simultaneous = sum(1 for row in rows if sum(not n.get("stale", True) for n in row.get("nodes") or []) >= 2)
    moving = sum(1 for row in rows if "moving" in str((row.get("classification") or {}).get("motion_level", "")).lower())
    return {
        "schema": "ruview-placement-summary-v1",
        "sample_count": len(rows),
        "simultaneous_two_plus_node_ratio": simultaneous / len(rows) if rows else 0.0,
        "moving_ratio": moving / len(rows) if rows else 0.0,
        "nodes": node_summary,
        "vitals": vital_summary,
        "warning": "Experimental engineering evidence only; not a medical measurement or diagnosis.",
    }


def geometry_readiness(doc: dict[str, Any]) -> dict[str, Any]:
    frame = doc.get("coordinate_frame") if isinstance(doc.get("coordinate_frame"), dict) else {}
    transmitter = doc.get("transmitter") if isinstance(doc.get("transmitter"), dict) else {}
    nodes = [n for n in doc.get("nodes") or [] if isinstance(n, dict)]
    surveyed = [n for n in nodes if n.get("surveyed") is True and _valid_position(n.get("position_m"))]
    distinct = {tuple(float(x) for x in n["position_m"]) for n in surveyed}
    valid_ids = [n.get("node_id") for n in surveyed]
    unique_ids = all(type(i) is int and 0 <= i <= 255 for i in valid_ids) and len(set(valid_ids)) == len(valid_ids)
    positions = list(distinct)
    # XY floor-plan triangle area: vertically spaced but collinear receivers do
    # not establish a 2D room-localization baseline. 0.05 m^2 is only a
    # degeneracy guard, NOT a claim of sufficient localization accuracy.
    max_area = max((abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])) / 2
                    for a in positions for b in positions for c in positions), default=0.0)
    floor_non_collinear = max_area >= 0.05
    schema_ok = doc.get("schema") == GEOMETRY_SCHEMA
    room_metric = bool(frame.get("metric") and frame.get("calibrated"))
    tx_surveyed = transmitter.get("surveyed") is True and _valid_position(transmitter.get("position_m"))
    geometry_ready = schema_ok and unique_ids and room_metric and tx_surveyed and len(distinct) >= 2
    phase_reference = str(doc.get("phase_reference") or "unknown")
    synchronized = phase_reference in {"shared_clock", "calibrated_phase", "time_of_flight_calibrated"}
    ranging_model = bool(doc.get("ranging_model_calibrated", False))
    # Conservative: three surveyed receivers, calibrated room, synchronized/ranging evidence.
    metric_triangulation_ready = geometry_ready and len(distinct) >= 3 and floor_non_collinear and synchronized and ranging_model
    return {
        "schema_ok": schema_ok,
        "surveyed_receiver_count": len(distinct),
        "surveyed_receiver_ids_unique": unique_ids,
        "max_floor_triangle_area_m2": round(max_area, 4),
        "floor_non_collinear": floor_non_collinear,
        "room_metric_calibrated": room_metric,
        "transmitter_surveyed": tx_surveyed,
        "phase_reference": phase_reference,
        "multilink_geometry_ready": geometry_ready,
        "metric_triangulation_ready": metric_triangulation_ready,
        "reason": (
            "metric triangulation requires valid schema, >=3 uniquely identified surveyed receivers with non-collinear XY floor geometry, a surveyed transmitter, calibrated metric room frame, and synchronized/calibrated ranging; independent-clock amplitude-only ESP32 fusion is not triangulation"
            if not metric_triangulation_ready else "metric triangulation prerequisites declared; held-out ground-truth validation is still required"
        ),
    }


def _valid_position(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 3 and all(_num(x) is not None for x in value)


@dataclass(frozen=True)
class ReferenceRow:
    timestamp_s: float
    heart_rate_bpm: float | None
    breathing_rate_bpm: float | None


def _parse_time(value: str) -> float:
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_reference_csv(path: Path) -> list[ReferenceRow]:
    rows: list[ReferenceRow] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append(ReferenceRow(
                timestamp_s=_parse_time(row["timestamp"]),
                heart_rate_bpm=_num(float(row["heart_rate_bpm"])) if row.get("heart_rate_bpm", "").strip() else None,
                breathing_rate_bpm=_num(float(row["breathing_rate_bpm"])) if row.get("breathing_rate_bpm", "").strip() else None,
            ))
    return sorted(rows, key=lambda row: row.timestamp_s)


def agreement(rows: list[dict[str, Any]], refs: list[ReferenceRow], *, tolerance_s: float = 2.0) -> dict[str, Any]:
    result = {"schema": "ruview-vitals-agreement-v1", "reference_rows": len(refs), "capture_rows": len(rows)}
    for field in ("heart_rate_bpm", "breathing_rate_bpm"):
        errors: list[float] = []
        paired = 0
        estimate_available = 0
        for row in rows:
            estimate = _num((row.get("vital_signs") or {}).get(field))
            if estimate is None:
                continue
            estimate_available += 1
            ts = _num(row.get("capture_unix_s"))
            if ts is None or not refs:
                continue
            ref = min(refs, key=lambda x: abs(x.timestamp_s - ts))
            ref_value = getattr(ref, field)
            if ref_value is None or abs(ref.timestamp_s - ts) > tolerance_s:
                continue
            paired += 1
            errors.append(estimate - ref_value)
        abs_errors = [abs(e) for e in errors]
        result[field] = {
            "estimate_coverage_ratio": estimate_available / len(rows) if rows else 0.0,
            "paired_count": paired,
            "paired_ratio": paired / len(rows) if rows else 0.0,
            "mae_bpm": statistics.fmean(abs_errors) if abs_errors else None,
            "rmse_bpm": math.sqrt(statistics.fmean([e * e for e in errors])) if errors else None,
            "bias_bpm": statistics.fmean(errors) if errors else None,
        }
    result["warning"] = "Agreement metrics are engineering validation only and do not establish clinical accuracy."
    return result


def capture(args: argparse.Namespace) -> int:
    token = load_token(args.token_file)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + args.duration
    failures = 0
    samples = 0
    with output.open("a", encoding="utf-8") as handle:
        while time.monotonic() < deadline:
            started = time.monotonic()
            try:
                row = sanitize_snapshot(fetch_json(args.url, token), label=args.label)
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                samples += 1
            except Exception as exc:
                failures += 1
                print(f"capture warning: {type(exc).__name__}", file=sys.stderr)
            time.sleep(max(0.0, args.interval - (time.monotonic() - started)))
    print(json.dumps({"output": str(output), "samples": samples, "failures": failures}, sort_keys=True))
    return 0 if samples else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--url", default="http://127.0.0.1:3000")
    cap.add_argument("--token-file")
    cap.add_argument("--output", required=True)
    cap.add_argument("--duration", type=float, default=60.0)
    cap.add_argument("--interval", type=float, default=1.0)
    cap.add_argument("--label", default="")
    summ = sub.add_parser("summary")
    summ.add_argument("jsonl")
    geom = sub.add_parser("geometry")
    geom.add_argument("manifest")
    agr = sub.add_parser("agreement")
    agr.add_argument("jsonl")
    agr.add_argument("reference_csv")
    agr.add_argument("--tolerance-s", type=float, default=2.0)
    args = parser.parse_args()
    if args.command == "capture":
        return capture(args)
    if args.command == "summary":
        print(json.dumps(summarize(read_jsonl(Path(args.jsonl))), indent=2, sort_keys=True))
        return 0
    if args.command == "geometry":
        print(json.dumps(geometry_readiness(json.loads(Path(args.manifest).read_text())), indent=2, sort_keys=True))
        return 0
    if args.command == "agreement":
        print(json.dumps(agreement(read_jsonl(Path(args.jsonl)), read_reference_csv(Path(args.reference_csv)), tolerance_s=args.tolerance_s), indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())