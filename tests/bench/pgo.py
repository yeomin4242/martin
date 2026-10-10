#!/usr/bin/env python3
"""Build and measure Martin with PGO, optionally followed by LLVM BOLT."""

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures"


def run(command, *, env=None, log=None):
    print("Running:", " ".join(map(str, command)), flush=True)
    started = time.monotonic()
    if log is None:
        subprocess.run(command, cwd=ROOT, env=env, check=True)
    else:
        with log.open("w") as output:
            subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
    return time.monotonic() - started


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def host_target():
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    return next(
        line.removeprefix("host: ")
        for line in output.splitlines()
        if line.startswith("host: ")
    )


def bolt_options(drop_veneers, optimizing=False):
    if not drop_veneers:
        return []
    # cargo-pgo replaces (rather than appends to) its defaults with --bolt-args.
    defaults = (
        "-reorder-blocks=ext-tsp -reorder-functions=hfsort -split-functions=2 "
        "-split-all-cold -jump-tables=move -use-gnu-stack -split-eh -lite=1 "
        "-icf=1 -relocs -update-debug-sections -dyno-stats"
        if optimizing
        else "-update-debug-sections"
    )
    return ["--bolt-args", defaults + " --drop-cortex-a53-843419-veneers"]


def prepare_fixtures():
    for name in ("world_cities", "world_cities_modified"):
        sql = FIXTURES / "mbtiles" / (name + ".sql")
        destination = sql.with_suffix(".mbtiles")
        # Recreate in a temporary file: interrupted preparation never leaves a
        # half-initialized database for the next run.
        temporary = destination.with_suffix(".mbtiles.tmp")
        temporary.unlink(missing_ok=True)
        with sqlite3.connect(temporary) as connection:
            connection.executescript(sql.read_text())
        temporary.replace(destination)
    # The tiny uncompressed_mvt fixture has a two-byte payload. Use the real
    # world-cities root tile instead so the compression scenario does work.
    with sqlite3.connect(FIXTURES / "mbtiles/world_cities.mbtiles") as connection:
        payload = connection.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level=0"
        ).fetchone()[0]
    destination = FIXTURES / "mbtiles/pgo_uncompressed.mbtiles"
    temporary = destination.with_suffix(".mbtiles.tmp")
    temporary.unlink(missing_ok=True)
    with sqlite3.connect(temporary) as connection:
        connection.executescript(
            "CREATE TABLE metadata (name TEXT, value TEXT); "
            "CREATE TABLE tiles (zoom_level INTEGER, tile_column INTEGER, "
            "tile_row INTEGER, tile_data BLOB); "
            "CREATE UNIQUE INDEX tile_index ON tiles(zoom_level,tile_column,tile_row);"
        )
        connection.executemany(
            "INSERT INTO metadata VALUES (?, ?)",
            [
                ("format", "pbf"),
                ("name", "PGO uncompressed cities"),
                ("minzoom", "0"),
                ("maxzoom", "0"),
            ],
        )
        connection.execute(
            "INSERT INTO tiles VALUES (0, 0, 0, ?)", (gzip.decompress(payload),)
        )
    temporary.replace(destination)


def build_identity(args, target, flags):
    digest = hashlib.sha256()
    tracked = (
        subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
        .decode()
        .split("\0")
    )
    for name in tracked:
        path = ROOT / name
        if name and (
            path.suffix in (".rs", ".sql", ".pmtiles")
            or path.name in ("Cargo.toml", "Cargo.lock")
            or name.startswith("martin/martin-ui/")
        ):
            digest.update(name.encode())
            digest.update(path.read_bytes())
    return {
        "sources_sha256": digest.hexdigest(),
        "target": target,
        "flags": flags,
        "features": args.features,
        "optimization": args.optimization,
        "bolt_drop_cortex_a53_veneers": args.bolt_drop_cortex_a53_veneers,
        "rustc": subprocess.check_output(["rustc", "-vV"], text=True),
        "database_sha256": hashlib.sha256(
            (args.database_url or "").encode()
        ).hexdigest(),
    }


def workloads(database_url):
    files = [
        str(FIXTURES / "mbtiles" / (name + ".mbtiles"))
        for name in ("world_cities", "world_cities_modified", "pgo_uncompressed")
    ]
    files.append(str(FIXTURES / "pmtiles/png.pmtiles"))
    with sqlite3.connect(files[0]) as connection:
        coordinates = [
            (z, x, (1 << z) - 1 - y)
            for z, x, y in connection.execute(
                "SELECT zoom_level, tile_column, tile_row FROM tiles "
                "ORDER BY zoom_level, tile_column, tile_row"
            )
        ]
    cases = [
        ("mbtiles-miss", "world_cities", coordinates, 0, files, []),
        ("pmtiles-miss", "png", [(0, 0, 0)], 0, files, []),
        (
            "composite-miss",
            "world_cities,world_cities_modified",
            coordinates,
            0,
            files,
            [],
        ),
        ("cache-hit", "world_cities", coordinates[:8], 64, files, []),
        (
            "compression-miss",
            "pgo_uncompressed",
            [(0, 0, 0)],
            0,
            files,
            ["-H", "Accept-Encoding: gzip"],
        ),
    ]
    if database_url:
        cases.extend(
            [
                (
                    "postgres-function-miss",
                    "function_zxy_query",
                    [(0, 0, 0)],
                    0,
                    [database_url],
                    [],
                ),
                (
                    "postgres-table-miss",
                    "table_source",
                    [(0, 0, 0)],
                    0,
                    [database_url],
                    [],
                ),
            ]
        )
    return cases


@contextlib.contextmanager
def server(binary, sources, cache, workers, directory, env):
    # Binding to port zero avoids touching an existing Martin instance.
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    config = directory / "config.json"
    configuration = {
        "listen_addresses": f"127.0.0.1:{port}",
        "worker_processes": workers,
        "cache": {"size_mb": cache},
        "shutdown_timeout": 5,
    }
    for source in sources:
        if source.startswith(("postgres://", "postgresql://")):
            configuration["postgres"] = {"connection_string": source}
        else:
            kind = Path(source).suffix.removeprefix(".")
            configuration.setdefault(kind, {"paths": []})["paths"].append(source)
    write_json(config, configuration)
    with (directory / "server.log").open("w") as log:
        process = subprocess.Popen(
            [str(binary), "--config", str(config)],
            cwd=ROOT,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"Martin exited; see {directory / 'server.log'}")
                try:
                    with urllib.request.urlopen(
                        base + "/health", timeout=1
                    ) as response:
                        if response.status == 200:
                            break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.1)
            else:
                raise RuntimeError(f"Martin did not become healthy; see {directory}")
            yield base
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
            try:
                status = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise RuntimeError(
                    "Martin did not shut down; profiling data may be incomplete"
                )
            if status != 0:
                raise RuntimeError(
                    f"Martin exited with status {status}; see {directory}"
                )


def check_result(result):
    statuses = result["statusCodeDistribution"]
    if not statuses or any(int(code) not in (200, 204) for code in statuses):
        raise RuntimeError(f"Unexpected HTTP statuses: {statuses}")
    if result.get("errorDistribution"):
        raise RuntimeError(f"Load test errors: {result['errorDistribution']}")
    if result["summary"]["successRate"] != 1:
        raise RuntimeError("Load test did not complete every request successfully")


def evaluation_coordinates(coordinates):
    # Preserve the original held-out split before excluding gzip(empty) tiles.
    # Otherwise most evaluation URLs exercise only the HTTP 204 fast path.
    with sqlite3.connect(FIXTURES / "mbtiles/world_cities.mbtiles") as connection:
        populated = {
            (z, x, (1 << z) - 1 - y)
            for z, x, y, data in connection.execute(
                "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles"
            )
            if gzip.decompress(data)
        }
    selected = [coord for coord in coordinates[1::2] if coord in populated]
    if not selected:
        raise RuntimeError("MBTiles fixture has no populated held-out tiles")
    return selected


def exercise(binary, args, directory, phase, env=None):
    results = {}
    for name, source, coordinates, cache, sources, headers in workloads(
        args.database_url
    ):
        case_dir = directory / name
        case_dir.mkdir(parents=True, exist_ok=True)
        # Where there are enough tiles, evaluate on coordinates not used in
        # training. Single-tile fixtures cannot provide a held-out workload.
        selected = coordinates[::2] if phase == "train" else coordinates[1::2]
        selected = selected or coordinates
        if phase == "bench" and name in (
            "mbtiles-miss",
            "composite-miss",
            "cache-hit",
        ):
            selected = evaluation_coordinates(coordinates)
        with server(binary, sources, cache, args.workers, case_dir, env) as base:
            urls = case_dir / "urls.txt"
            urls.write_text(
                "".join(f"{base}/{source}/{z}/{x}/{y}\n" for z, x, y in selected)
            )
            # Validate and save a response fingerprint before measuring; this
            # catches optimized binaries that return different tile contents.
            request = urllib.request.Request(urls.read_text().splitlines()[0])
            for index in range(0, len(headers), 2):
                key, value = headers[index + 1].split(":", 1)
                request.add_header(key, value.strip())
            with urllib.request.urlopen(request, timeout=30) as response:
                fingerprint = {
                    "status": response.status,
                    "sha256": hashlib.sha256(response.read()).hexdigest(),
                }
            command = [
                args.oha,
                "--no-tui",
                "--output-format",
                "json",
                "--urls-from-file",
                "-c",
                str(args.connections),
                "-t",
                "30s",
                *headers,
                str(urls),
            ]
            warmup = case_dir / "warmup.json"
            run([*command, "-n", "500", "-o", str(warmup)])
            check_result(json.loads(warmup.read_text()))
            output = case_dir / "oha.json"
            if phase == "train":
                load = ["-n", str(args.training_requests)]
            else:
                load = [
                    "-z",
                    f"{args.seconds}s",
                    "--wait-ongoing-requests-after-deadline",
                ]
            run([*command, *load, "-o", str(output)])
            result = json.loads(output.read_text())
            check_result(result)
            results[name] = {
                "requests_per_second": result["summary"]["requestsPerSec"],
                "p95_seconds": result["latencyPercentiles"]["p95"],
                "fingerprint": fingerprint,
            }
    return results


def benchmark(binaries, args, output):
    measurements = {label: [] for label in binaries}
    labels = list(binaries)
    for repeat in range(args.repeats):
        # Rotate the order to avoid giving one binary all the cold or hot runs.
        order = labels[repeat % len(labels) :] + labels[: repeat % len(labels)]
        for label in order:
            print(f"Benchmark {label}, repetition {repeat + 1}", flush=True)
            directory = output / label / str(repeat + 1)
            measurements[label].append(
                exercise(binaries[label], args, directory, "bench")
            )
            write_json(output / "measurements.json", measurements)
    baseline = labels[0]
    reference = measurements[baseline][0]
    summary = {}
    for label, runs in measurements.items():
        summary[label] = {}
        for name in reference:
            if any(
                run[name]["fingerprint"] != reference[name]["fingerprint"]
                for run in runs
            ):
                raise RuntimeError(f"Response contents changed for {label}/{name}")
            throughput = [run[name]["requests_per_second"] for run in runs]
            p95 = [run[name]["p95_seconds"] for run in runs]
            baseline_rps = statistics.median(
                run[name]["requests_per_second"] for run in measurements[baseline]
            )
            baseline_p95 = statistics.median(
                run[name]["p95_seconds"] for run in measurements[baseline]
            )
            summary[label][name] = {
                "median_requests_per_second": statistics.median(throughput),
                "min_requests_per_second": min(throughput),
                "max_requests_per_second": max(throughput),
                "median_p95_seconds": statistics.median(p95),
                "p95_change_percent": (statistics.median(p95) / baseline_p95 - 1) * 100,
                "throughput_change_percent": (
                    statistics.median(throughput) / baseline_rps - 1
                )
                * 100,
            }
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, indent=2))


def build(args):
    host = host_target()
    target = args.target or host
    if target != host:
        raise ValueError(
            f"Profiling must execute natively: requested {target}, host {host}"
        )
    if args.optimization == "pgo-bolt" and platform.system() != "Linux":
        raise ValueError(
            "BOLT requires a Linux ELF binary; run this experiment on Linux"
        )
    output = args.output.resolve()
    if output.exists() and not args.resume:
        raise ValueError(
            f"Use a fresh output directory to avoid stale profiles: {output}"
        )
    output.mkdir(parents=True, exist_ok=args.resume)
    target_dir = output / "build"
    profile_dir = target_dir / "pgo-profiles"
    env = os.environ.copy()
    if "CARGO_ENCODED_RUSTFLAGS" in env:
        raise ValueError(
            "Unset CARGO_ENCODED_RUSTFLAGS so cargo-pgo can add instrumentation flags"
        )
    flags = env.get("RUSTFLAGS", "") + " -C strip=none"
    if target.startswith("x86_64") and not args.legacy_cpu:
        flags += " -C target-cpu=x86-64-v3"
    env["RUSTFLAGS"] = flags.strip()
    identity = build_identity(args, target, env["RUSTFLAGS"])
    identity_file = output / "identity.json"
    if args.resume:
        if (
            not identity_file.exists()
            or json.loads(identity_file.read_text()) != identity
        ):
            raise ValueError(
                "Cannot resume: sources, compiler, database or build settings changed"
            )
    else:
        write_json(identity_file, identity)
    env["LLVM_PROFILE_FILE"] = str(profile_dir / "martin-%m-%p.profraw")
    cargo_args = [
        "--locked",
        "--target",
        target,
        "--target-dir",
        str(target_dir),
        "--package",
        "martin",
        "--bin",
        "martin",
    ]
    if args.features:
        cargo_args.extend(["--no-default-features", "--features", args.features])
    binary = target_dir / target / "release/martin"
    timings_file = output / "timings.json"
    timings = (
        json.loads(timings_file.read_text())
        if args.resume and timings_file.exists()
        else {}
    )
    binaries = {}

    def compile_stage(label, command, artifact=binary):
        saved = output / label / "martin"
        # Only the profile-independent builds can be reused. Downstream stages
        # must always consume profiles collected by this invocation.
        if args.resume and label in ("baseline", "instrumented") and saved.exists():
            return saved
        timings[label] = run(command, env=env, log=output / (label + ".log"))
        write_json(timings_file, timings)
        saved.parent.mkdir(exist_ok=args.resume)
        shutil.copy2(artifact, saved)
        return saved

    if args.compare:
        binaries["baseline"] = compile_stage(
            "baseline", ["cargo", "build", "--release", *cargo_args]
        )
    instrumented = compile_stage(
        "instrumented",
        [
            "cargo",
            "pgo",
            "build",
            "--profiles-dir",
            str(profile_dir),
            "--",
            *cargo_args,
        ],
    )
    started = time.monotonic()
    for raw in profile_dir.glob("*.profraw"):
        raw.unlink()
    exercise(instrumented, args, output / "training", "train", env)
    timings["pgo_training"] = time.monotonic() - started
    if not any(profile_dir.glob("*.profraw")):
        raise RuntimeError(
            "No PGO profiles were flushed after graceful server shutdown"
        )
    binaries["pgo"] = compile_stage(
        "pgo",
        [
            "cargo",
            "pgo",
            "optimize",
            "--profiles-dir",
            str(profile_dir),
            "--",
            *cargo_args,
        ],
    )
    if args.optimization == "pgo-bolt":
        # --with-pgo resolves profiles relative to --target-dir. Every run has
        # its own build directory, so another run cannot overwrite its data.
        # cargo-pgo 0.3.0 only supports the default BOLT profile directory.
        bolt_dir = target_dir / "bolt-profiles"
        instrumented = compile_stage(
            "bolt-instrumented",
            [
                "cargo",
                "pgo",
                "bolt",
                "build",
                "--with-pgo",
                *bolt_options(args.bolt_drop_cortex_a53_veneers),
                "--",
                *cargo_args,
            ],
            binary.with_name("martin-bolt-instrumented"),
        )
        started = time.monotonic()
        exercise(instrumented, args, output / "bolt-training", "train", env)
        timings["bolt_training"] = time.monotonic() - started
        if not any(bolt_dir.rglob("*.fdata")):
            raise RuntimeError(
                "No BOLT profiles were flushed after graceful server shutdown"
            )
        binaries["pgo-bolt"] = compile_stage(
            "pgo-bolt",
            [
                "cargo",
                "pgo",
                "bolt",
                "optimize",
                "--with-pgo",
                *bolt_options(args.bolt_drop_cortex_a53_veneers, optimizing=True),
                "--",
                *cargo_args,
            ],
            binary.with_name("martin-bolt-optimized"),
        )
    final = binaries[args.optimization]
    # Strip only after BOLT has finished reading symbols and relocations.
    shipped = output / "martin"
    shutil.copy2(final, shipped)
    run(["strip", str(shipped)])
    write_json(
        output / "build.json",
        {
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "rustc": subprocess.check_output(["rustc", "-vV"], text=True),
            "oha": subprocess.check_output([args.oha, "--version"], text=True).strip(),
            "platform": platform.platform(),
            "bolt": subprocess.check_output(
                ["llvm-bolt", "--version"], text=True
            ).splitlines()[0]
            if args.optimization == "pgo-bolt"
            else None,
            "target": target,
            "optimization": args.optimization,
            "bolt_drop_cortex_a53_veneers": args.bolt_drop_cortex_a53_veneers,
            "rustflags": env["RUSTFLAGS"],
            "features": args.features or "default",
            "timings_seconds": timings,
            "binary_sizes_bytes": {
                label: path.stat().st_size for label, path in binaries.items()
            },
            "shipped_size_bytes": shipped.stat().st_size,
            "postgres_included": bool(args.database_url),
            "workload": {
                "workers": args.workers,
                "connections": args.connections,
                "training_requests": args.training_requests,
                "seconds": args.seconds,
                "repeats": args.repeats,
            },
        },
    )
    # Smoke-test the actual stripped artifact even when comparison is disabled.
    exercise(shipped, args, output / "smoke", "train")
    if args.compare:
        # Match shipping strip policy for every measured candidate.
        for path in binaries.values():
            run(["strip", str(path)])
        benchmark(binaries, args, output / "benchmark")


def positive(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "bench"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--optimization", choices=("pgo", "pgo-bolt"), default="pgo")
    parser.add_argument("--target")
    parser.add_argument(
        "--features", help="explicit feature set instead of default features"
    )
    parser.add_argument("--legacy-cpu", action="store_true")
    parser.add_argument(
        "--bolt-drop-cortex-a53-veneers",
        action="store_true",
        help="ARM experiment only: excludes Cortex-A53 support; never use for general ARM releases",
    )
    parser.add_argument("--compare", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse baseline/instrumented builds from an interrupted experiment",
    )
    parser.add_argument("--binary", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--database-url", default=os.environ.get("PGO_DATABASE_URL"))
    parser.add_argument("--oha", default="oha")
    parser.add_argument("--workers", type=positive, default=2)
    parser.add_argument("--connections", type=positive, default=32)
    parser.add_argument("--training-requests", type=positive, default=20000)
    parser.add_argument("--seconds", type=positive, default=10)
    parser.add_argument("--repeats", type=positive, default=5)
    args = parser.parse_args()
    os.chdir(ROOT)
    prepare_fixtures()
    if args.command == "build":
        build(args)
    else:
        binaries = {}
        for item in args.binary:
            label, path = item.split("=", 1)
            if (
                not label
                or Path(label).name != label
                or label in (".", "..")
                or label in binaries
            ):
                parser.error("binary labels must be unique directory names")
            binaries[label] = Path(path).resolve()
        if not binaries:
            parser.error("bench requires at least one --binary LABEL=PATH")
        args.output.mkdir(parents=True, exist_ok=False)
        benchmark(binaries, args, args.output.resolve())


if __name__ == "__main__":
    main()
