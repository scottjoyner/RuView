#!/usr/bin/env python3
from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("placement", ROOT / "scripts" / "ruview_placement_vitals.py")
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
assert SPEC and SPEC.loader
SPEC.loader.exec_module(mod)


def sample(node2_stale=False, hr=72.0, br=14.0):
    return {
        "source": "esp32", "tick": 1, "estimated_persons": 1,
        "classification": {"presence": True, "confidence": 0.9, "motion_level": "present_still"},
        "vital_signs": {"heart_rate_bpm": hr, "heartbeat_confidence": 0.8, "breathing_rate_bpm": br, "breathing_confidence": 0.9, "signal_quality": 0.85},
        "node_features": [
            {"node_id": 1, "rssi_dbm": -42, "last_seen_ms": 20, "frame_rate_hz": 30, "stale": False, "classification": {"presence": True, "confidence": 0.9, "motion_level": "present_still"}, "features": {"motion_band_power": 1, "breathing_band_power": 2, "variance": 3}},
            {"node_id": 2, "rssi_dbm": -38, "last_seen_ms": 30, "frame_rate_hz": 31, "stale": node2_stale, "classification": {"presence": True, "confidence": 0.85, "motion_level": "present_still"}, "features": {"motion_band_power": 1, "breathing_band_power": 2, "variance": 3}}
        ]
    }


class PlacementVitalsTests(unittest.TestCase):
    def test_sanitized_summary_tracks_two_node_freshness(self):
        rows = [mod.sanitize_snapshot(sample()), mod.sanitize_snapshot(sample(node2_stale=True))]
        summary = mod.summarize(rows)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["simultaneous_two_plus_node_ratio"], 0.5)
        self.assertEqual(summary["nodes"]["1"]["fresh_ratio"], 1.0)
        self.assertEqual(summary["nodes"]["2"]["fresh_ratio"], 0.5)
        self.assertAlmostEqual(summary["nodes"]["1"]["rssi_dbm"]["mean"], -42.0)

    def test_independent_clock_two_node_geometry_never_claims_triangulation(self):
        doc = {
            "schema": mod.GEOMETRY_SCHEMA,
            "coordinate_frame": {"metric": True, "calibrated": True},
            "transmitter": {"surveyed": True, "position_m": [0, 0, 1]},
            "nodes": [
                {"node_id": 1, "surveyed": True, "position_m": [1, 0, 1]},
                {"node_id": 2, "surveyed": True, "position_m": [0, 1, 1]},
            ],
            "phase_reference": "independent_clocks",
            "ranging_model_calibrated": False,
        }
        ready = mod.geometry_readiness(doc)
        self.assertTrue(ready["multilink_geometry_ready"])
        self.assertFalse(ready["metric_triangulation_ready"])

    def test_even_three_nodes_require_synchronized_calibrated_ranging(self):
        base = {
            "schema": mod.GEOMETRY_SCHEMA,
            "coordinate_frame": {"metric": True, "calibrated": True},
            "transmitter": {"surveyed": True, "position_m": [0, 0, 1]},
            "nodes": [{"node_id": i, "surveyed": True, "position_m": [i, i % 2, 1]} for i in (1, 2, 3)],
            "phase_reference": "independent_clocks", "ranging_model_calibrated": False,
        }
        self.assertFalse(mod.geometry_readiness(base)["metric_triangulation_ready"])
        base["phase_reference"] = "calibrated_phase"
        base["ranging_model_calibrated"] = True
        self.assertTrue(mod.geometry_readiness(base)["metric_triangulation_ready"])

    def test_collinear_three_node_geometry_is_not_metric_triangulation(self):
        base = {
            "schema": mod.GEOMETRY_SCHEMA,
            "coordinate_frame": {"metric": True, "calibrated": True},
            "transmitter": {"surveyed": True, "position_m": [0, 2, 1.5]},
            "nodes": [{"node_id": i, "surveyed": True, "position_m": [i, 0, 1]} for i in (1, 2, 3)],
            "phase_reference": "calibrated_phase", "ranging_model_calibrated": True,
        }
        report = mod.geometry_readiness(base)
        self.assertEqual(report["surveyed_receiver_count"], 3)
        self.assertFalse(report["floor_non_collinear"])
        self.assertEqual(report["max_floor_triangle_area_m2"], 0)
        self.assertFalse(report["metric_triangulation_ready"])
        base["nodes"][2]["position_m"][1] = 1
        report = mod.geometry_readiness(base)
        self.assertTrue(report["floor_non_collinear"])
        self.assertTrue(report["metric_triangulation_ready"])

    def test_reused_node_id_cannot_fake_three_receivers(self):
        doc = {
            "schema": mod.GEOMETRY_SCHEMA,
            "coordinate_frame": {"metric": True, "calibrated": True},
            "transmitter": {"surveyed": True, "position_m": [0, 2, 1.5]},
            "nodes": [
                {"node_id": 1, "surveyed": True, "position_m": [0, 0, 1]},
                {"node_id": 2, "surveyed": True, "position_m": [2, 0, 1]},
                {"node_id": 2, "surveyed": True, "position_m": [0, 2, 1]},
            ],
            "phase_reference": "calibrated_phase", "ranging_model_calibrated": True,
        }
        report = mod.geometry_readiness(doc)
        self.assertFalse(report["surveyed_receiver_ids_unique"])
        self.assertFalse(report["metric_triangulation_ready"])
        doc["nodes"][2]["node_id"] = 3
        doc["schema"] = "unknown-schema"
        self.assertFalse(mod.geometry_readiness(doc)["metric_triangulation_ready"])

    def test_reference_agreement_reports_error_and_coverage(self):
        rows = []
        for ts, hr in [(100.0, 70.0), (101.0, 74.0), (102.0, None)]:
            row = mod.sanitize_snapshot(sample(hr=hr if hr is not None else 72.0))
            row["capture_unix_s"] = ts
            if hr is None:
                row["vital_signs"]["heart_rate_bpm"] = None
            rows.append(row)
        refs = [mod.ReferenceRow(100.0, 72.0, 14.0), mod.ReferenceRow(101.0, 72.0, 14.0), mod.ReferenceRow(102.0, 72.0, 14.0)]
        report = mod.agreement(rows, refs, tolerance_s=0.2)
        hr = report["heart_rate_bpm"]
        self.assertEqual(hr["paired_count"], 2)
        self.assertAlmostEqual(hr["mae_bpm"], 2.0)
        self.assertAlmostEqual(hr["bias_bpm"], 0.0)
        self.assertAlmostEqual(hr["estimate_coverage_ratio"], 2 / 3)


if __name__ == "__main__":
    unittest.main()