"""Failure checks for the performance experiment (no Rust build required)."""

import argparse
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pgo


class BenchmarkChecks(unittest.TestCase):
    def test_evaluation_excludes_empty_tiles_without_changing_held_out_split(self):
        with tempfile.TemporaryDirectory() as directory:
            fixtures = Path(directory)
            (fixtures / "mbtiles").mkdir()
            with sqlite3.connect(fixtures / "mbtiles/world_cities.mbtiles") as c:
                c.execute(
                    "CREATE TABLE tiles (zoom_level, tile_column, tile_row, tile_data)"
                )
                c.executemany(
                    "INSERT INTO tiles VALUES (2, ?, 0, ?)",
                    [
                        (0, gzip.compress(b"train")),
                        (1, gzip.compress(b"")),
                        (2, gzip.compress(b"train")),
                        (3, gzip.compress(b"test")),
                    ],
                )
            with patch.object(pgo, "FIXTURES", fixtures):
                self.assertEqual(
                    pgo.evaluation_coordinates([(2, x, 3) for x in range(4)]),
                    [(2, 3, 3)],
                )
                with self.assertRaisesRegex(RuntimeError, "no populated held-out"):
                    pgo.evaluation_coordinates([(2, 0, 3), (2, 1, 3)])

    def test_arm_bolt_override_retains_optimization_defaults(self):
        self.assertEqual(pgo.bolt_options(False), [])
        flags = pgo.bolt_options(True, optimizing=True)
        self.assertEqual(flags[0], "--bolt-args")
        self.assertIn("-reorder-blocks=ext-tsp", flags[1])
        self.assertIn("-update-debug-sections", flags[1])
        self.assertIn("--drop-cortex-a53-843419-veneers", flags[1])

    def test_server_uses_config_sources_and_flushes_on_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            binary = folder / "fake-martin"
            observed = folder / "observed.json"
            binary.write_text("""#!/usr/bin/env python3
import json, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
assert len(sys.argv) == 3 and sys.argv[1] == "--config"
config = json.loads(Path(sys.argv[2]).read_text())
observed = Path(sys.argv[2]).parent / "observed.json"
observed.write_text(json.dumps(config))
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, *_args):
        pass
def shutdown(*_args):
    observed.with_suffix(".flushed").write_text("graceful")
    sys.exit(0)
signal.signal(signal.SIGTERM, shutdown)
host, port = config["listen_addresses"].split(":")
HTTPServer((host, int(port)), Handler).serve_forever()
""")
            binary.chmod(0o755)
            with pgo.server(
                binary, ["/tiles/a.mbtiles", "/tiles/b.pmtiles"], 0, 2, folder, None
            ) as base:
                self.assertTrue(base.startswith("http://127.0.0.1:"))
                config = json.loads(observed.read_text())
                self.assertEqual(config["mbtiles"]["paths"], ["/tiles/a.mbtiles"])
                self.assertEqual(config["pmtiles"]["paths"], ["/tiles/b.pmtiles"])
            self.assertEqual(observed.with_suffix(".flushed").read_text(), "graceful")

    def test_http_errors_cannot_look_like_a_performance_improvement(self):
        result = {
            "statusCodeDistribution": {"200": 1, "500": 99},
            "errorDistribution": {},
            "summary": {"successRate": 1},
        }
        with self.assertRaisesRegex(RuntimeError, "HTTP statuses"):
            pgo.check_result(result)

    def test_transport_errors_are_rejected(self):
        result = {
            "statusCodeDistribution": {"200": 1},
            "errorDistribution": {"timeout": 1},
            "summary": {"successRate": 0.5},
        }
        with self.assertRaisesRegex(RuntimeError, "Load test errors"):
            pgo.check_result(result)

    def test_missing_responses_are_rejected(self):
        result = {
            "statusCodeDistribution": {},
            "errorDistribution": {},
            "summary": {"successRate": 1},
        }
        with self.assertRaises(RuntimeError):
            pgo.check_result(result)

    def test_empty_tiles_are_valid_responses(self):
        pgo.check_result(
            {
                "statusCodeDistribution": {"200": 2, "204": 1},
                "errorDistribution": {},
                "summary": {"successRate": 1},
            }
        )

    def test_changed_tile_contents_fail_the_comparison(self):
        def exercise(binary, *_args):
            return {
                "tiles": {
                    "fingerprint": {"sha256": str(binary)},
                    "requests_per_second": 100,
                    "p95_seconds": 0.01,
                }
            }

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(repeats=1)
            with (
                patch.object(pgo, "exercise", side_effect=exercise),
                self.assertRaisesRegex(RuntimeError, "Response contents changed"),
            ):
                pgo.benchmark(
                    {"baseline": Path("a"), "pgo": Path("b")}, args, Path(directory)
                )

    def test_repetition_order_rotates_and_summary_uses_medians(self):
        order = []

        def exercise(binary, *_args):
            order.append(str(binary))
            return {
                "tiles": {
                    "fingerprint": {"sha256": "same"},
                    "requests_per_second": 100 if str(binary) == "a" else 110,
                    "p95_seconds": 0.01,
                }
            }

        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(repeats=2)
            with (
                patch.object(pgo, "exercise", side_effect=exercise),
                patch("builtins.print"),
            ):
                pgo.benchmark(
                    {"baseline": Path("a"), "pgo": Path("b")}, args, Path(directory)
                )
            self.assertEqual(order, ["a", "b", "b", "a"])
            summary = json.loads((Path(directory) / "summary.json").read_text())
            self.assertAlmostEqual(
                summary["pgo"]["tiles"]["throughput_change_percent"], 10
            )

    def test_invalid_sample_counts_are_rejected(self):
        for value in ("0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                pgo.positive(value)


if __name__ == "__main__":
    unittest.main()
