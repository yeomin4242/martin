---
description: Build and compare Martin release binaries using PGO and LLVM BOLT
icon: material/speedometer
tags:
  - development
---

# Profile-guided optimization

The PGO experiment compares ordinary release builds with builds optimized using
real HTTP request profiles. It can also apply LLVM BOLT to the PGO binary.
Optimization is opt-in until measurements justify the additional build cost.
See [issue #1858](https://github.com/maplibre/martin/issues/1858).

## Run the experiment

The easiest Linux setup is the build environment in `.github/files/pgo.Dockerfile`.
It includes Rust, its matching LLVM profiling tools, cargo-pgo 0.3.0, LLVM BOLT 23,
Node.js 24 and oha 1.16.0. The image builds for both amd64 and arm64.

```sh
docker build -f .github/files/pgo.Dockerfile -t martin-pgo .
docker run --rm -v "$PWD:/work" martin-pgo build \
  --output target/pgo-experiment --optimization pgo --compare
```

Use `--optimization pgo-bolt` to compare release, PGO and PGO+BOLT. Every run
needs a new output directory so stale profiles cannot influence the experiment.
For an interrupted run, `--resume` reuses only baseline and instrumented builds
after checking source, compiler and build settings. It collects fresh profiles
and rebuilds downstream optimized artifacts.
The target must match the compiler host; cross-compilation is rejected. Run the
container on native hardware rather than using QEMU. x86_64 builds use the release CPU level,
`x86-64-v3`; use `--legacy-cpu` to test baseline x86-64 instead.

For PostgreSQL scenarios, initialize Martin's test fixtures in a disposable
PostGIS database, then pass `PGO_DATABASE_URL` to the container. On Linux, a
database bound to localhost can be reached with `--network host`:

```sh
docker run --rm --network host -v "$PWD:/work" \
  -e PGO_DATABASE_URL=postgres://postgres:postgres@127.0.0.1:5411/db \
  martin-pgo build --output target/pgo-with-postgres \
  --optimization pgo-bolt --compare
```

On Docker Desktop, use a Docker network shared with the database and its service
name, or `host.docker.internal` instead of localhost. The benchmark workflow uses
the multiarch rebuild `ghcr.io/baosystems/postgis:18-3.6`, because the upstream
PostGIS image only provides amd64. The database must be initialized with
`tests/fixtures/initdb.sh` before profiling.

For native execution, install cargo-pgo, oha and the LLVM tools first:

```sh
cargo install cargo-pgo --version 0.3.0 --locked
rustup component add llvm-tools-preview
cargo install oha --version 1.16.0 --locked
just bench-pgo pgo target/pgo-native
```

BOLT additionally requires Linux, `llvm-bolt` and `merge-fdata` on `PATH`.
On Ubuntu, `.github/files/install-bolt.sh` installs a requested LLVM major from
the signed [LLVM package repository](https://apt.llvm.org/). CI selects the LLVM
major reported by `rustc -vV`; an older BOLT may fail to process newer binaries.
macOS can collect PGO profiles but cannot run the ELF BOLT pipeline.

LLVM BOLT 23 rejects ARM binaries containing Cortex-A53 erratum 843419 veneers.
General ARM release packaging therefore uses PGO only, even when the Rust CI
workflow selects `pgo-bolt`; the benchmark workflow also measures PGO only on ARM.
For a controlled experiment on hardware known not to be Cortex-A53, pass
`--bolt-drop-cortex-a53-veneers`. This explicitly removes those workarounds and
excludes Cortex-A53 support. Do not distribute that artifact as a general ARM
release. The default preserves the guard and reports the BOLT error.

## What is measured

The workload covers MBTiles, PMTiles, composite sources, cache hits and gzip
compression with caching disabled. The compression fixture uses an uncompressed
copy of the world-cities root tile. PostgreSQL function and table sources are
included when `PGO_DATABASE_URL` is set. MBTiles fixtures are generated from the
SQL files already in the repository; no large external data download is needed.

Each server runs with two workers and each load generator uses 32 connections.
The defaults train with 20,000 requests per scenario, warm up with 500 requests,
and measure for ten seconds per scenario over five repetitions. Candidate order
rotates between repetitions. Change these with `--workers`, `--connections`,
`--training-requests`, `--seconds` and `--repeats`.
Cache-miss cases disable Martin's tile cache; operating-system and database
caches are warm after the warmup requests.

Where the fixture has multiple tile coordinates, alternating coordinates are
reserved for evaluation and excluded from training. Single-tile PMTiles,
compression and PostgreSQL fixtures cannot provide independent held-out data.
Use larger production-like datasets before enabling optimization by default.
Training includes the fixture's populated and empty tiles. MBTiles evaluation
excludes empty held-out tiles so throughput does not mostly measure HTTP 204
responses; the original train/evaluation coordinate split is preserved.

The harness rejects transport errors and HTTP statuses other than 200 and 204.
It also compares a sample response's status and content hash across builds.
Servers shut down gracefully to flush profiling data; missing profiles are a
failure. Symbols are stripped only after BOLT has finished.

The output directory contains:

- `build.json`: source revision, compiler version, flags, features, build and
  training times, and binary sizes. Phase timings share a Cargo directory, so
  dependency downloads and cache reuse must be considered when comparing costs.
- `identity.json` and `timings.json`: build identity and completed compile timings,
  also available when an experiment is interrupted before optimization finishes.
- `benchmark/summary.json`: median throughput and p95 latency, throughput range,
  and throughput change relative to the ordinary release baseline.
- `benchmark/measurements.json`: every repetition, including response hashes.
- Per-scenario `oha.json`, `warmup.json`, URL lists and server logs.
- Compiler logs, raw profiles, and the final stripped `martin` binary.

For an additional comparison without rebuilding:

```sh
python3 tests/bench/pgo.py bench --output target/pgo-remeasurement \
  --binary baseline=target/pgo-experiment/baseline/martin \
  --binary pgo=target/pgo-experiment/pgo/martin
```

Small changes need repeated measurements on an otherwise idle native machine.
A busy laptop, a shared CI runner, an emulated database or an overloaded load
generator can hide or imitate a few percent improvement. Check both throughput
and tail latency, including scenarios that did not improve.

## CI and release packaging

Run the **PGO and BOLT benchmark** workflow manually to collect comparable
results on native amd64 and arm64 runners. It uploads measurements, logs and
the optimized binary, including diagnostics from failed experiments.
Select `architecture: x86_64`, `aarch64` or `both` to limit a run. The workflow
also records the runner's CPU, memory and kernel information.

The **Rust CI** workflow accepts `optimization: none`, `pgo` or `pgo-bolt`.
The default is `none`. An enabled optimization applies to native Linux GNU base
release builds; PRs, musl, legacy musl, macOS, Windows, Debian and the rendering
variant retain their existing build paths. The separate mbtiles CLI is not
profile-optimized because the HTTP workload does not represent its usage.

`just build-release-pgo <target> <optimization>` builds the optimized Martin
binary, checks it with HTTP requests, and copies it into the normal release
artifact directory. The existing Dockerfile and image tests consume that binary
without adding compilers or profiles to the production image. Invalid target,
debug, musl or rendering configurations fail explicitly rather than quietly
shipping an unoptimized artifact.
Optimized base images include the `libfreetype6` runtime package because native
builds can link system FreeType. For local packaging with the existing Dockerfile,
pass `--build-arg EXTRA_PACKAGES=libfreetype6`; the Rust CI workflow passes this
argument for optimized base images automatically.

After reviewing benchmarks, the caller in `.github/workflows/ci.yml` can pass
`optimization: pgo` to the Rust workflow for main and release builds. Keep this
opt-in until the improvement and build-time tradeoff have been established for
both architectures. BOLT should be evaluated independently because its tooling
and additional profile pass have their own cost and compatibility constraints.
