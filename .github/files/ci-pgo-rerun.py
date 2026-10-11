#!/usr/bin/env python3
"""Execute the PR's unchanged CI shell blocks and measure their shipped binary."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / 'target/ci-final'
OUT.mkdir(parents=True, exist_ok=True)


def step(name):
    lines = (ROOT / '.github/workflows/build-binary.yml').read_text().splitlines()
    start = lines.index('      - name: ' + name)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith('      - ')), len(lines))
    block = lines[start:end]
    for index, line in enumerate(block):
        if line == '        run: |':
            return '\n'.join(line[10:] for line in block[index + 1:]) + '\n'
        if line.startswith('        run: '):
            return line[len('        run: '):] + '\n'
    raise AssertionError('Missing run block: ' + name)


def execute(label, command):
    started = time.monotonic()
    print('Running', label, flush=True)
    with (OUT / (label + '.log')).open('w') as log:
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            log.write(line)
            log.flush()
            print(line, end='', flush=True)
        status = proc.wait()
    timings = json.loads((OUT / 'timings.json').read_text()) if (OUT / 'timings.json').exists() else {}
    timings[label] = {'seconds': time.monotonic() - started, 'exit_status': status}
    (OUT / 'timings.json').write_text(json.dumps(timings, indent=2))
    if status:
        raise SystemExit(status)


phase = sys.argv[1]
snapshot = json.loads(Path(os.environ['RUNNER_TEMP'], 'ci-final-snapshot.json').read_text())
if phase == 'prepare':
    patch_path = Path(os.environ['RUNNER_TEMP'], 'ci-final-worktree.patch')
    assert hashlib.sha256(patch_path.read_bytes()).hexdigest() == snapshot['patch_sha256']
    subprocess.run(['git', 'apply', '--check', str(patch_path)], check=True)
    subprocess.run(['git', 'apply', str(patch_path)], check=True)
for path, expected in snapshot['files'].items():
    assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == expected, path
if phase == 'prepare':
    for label, name in [('install', 'Install cargo-pgo and LLVM tooling'), ('optimized', 'Build ${{ inputs.target }}')]:
        raw = step(name)
        (OUT / (label + '-source.sh')).write_text(raw)
        rendered = raw.replace('${{ inputs.pgo }}', 'true')
        assert '${{' not in rendered
        (OUT / (label + '.sh')).write_text(rendered)
    shutil.copy2(patch_path, OUT / 'worktree.patch')
    shutil.copy2(Path(os.environ['RUNNER_TEMP'], 'ci-final-snapshot.json'), OUT / 'snapshot.json')
    shutil.copy2(ROOT / 'justfile', OUT / 'snapshot-justfile')
    shutil.copy2(ROOT / '.github/workflows/build-binary.yml', OUT / 'snapshot-build-binary.yml')
    (OUT / 'baseline.sh').write_text('set -euo pipefail\njust build-release "$TARGET"\n')
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
    assert sha == os.environ['SOURCE_SHA'], (sha, os.environ['SOURCE_SHA'])
    (OUT / 'metadata.json').write_text(json.dumps({
        'commit': sha, 'worktree_snapshot': snapshot, 'target': os.environ['TARGET'],
        'optimization': 'pgo-bolt' if os.environ['TARGET'].startswith('x86_64') else 'pgo',
        'rustc': subprocess.check_output(['rustc', '-vV'], text=True),
        'baseline_cpu': 'x86-64-v3' if os.environ['TARGET'].startswith('x86_64') else 'default',
        'compiler_flags': 'baseline strip=symbols; optimized strip=none then strip shipped copies; same CPU level',
        'source_step_sha256': {name: hashlib.sha256((OUT / (name + '-source.sh')).read_bytes()).hexdigest() for name in ['install', 'optimized']},
        'step_sha256': {name: hashlib.sha256((OUT / (name + '.sh')).read_bytes()).hexdigest() for name in ['install', 'optimized']},
        'seconds': 10, 'repeats': 5, 'workers': 2, 'connections': 32,
    }, indent=2))
elif phase in ['install', 'baseline', 'optimized']:
    execute(phase, ['bash', str(OUT / (phase + '.sh'))])
    if phase != 'install':
        source = ROOT / os.environ['CARGO_TARGET_DIR'] / os.environ['TARGET'] / 'release/martin' if phase == 'baseline' else ROOT / 'target_releases/martin'
        destination = OUT / phase / 'martin'
        destination.parent.mkdir(exist_ok=True)
        shutil.copy2(source, destination)
        metadata = json.loads((OUT / 'metadata.json').read_text())
        metadata.setdefault('binary_sizes', {})[phase] = destination.stat().st_size
        if phase == 'optimized':
            metadata['oha'] = subprocess.check_output(['oha', '--version'], text=True).strip()
            metadata['bolt'] = subprocess.check_output(['llvm-bolt', '--version'], text=True).splitlines()[0] if metadata['optimization'] == 'pgo-bolt' else None
            profiles = list((ROOT / os.environ['CARGO_TARGET_DIR'] / 'pgo-profiles').glob('*.profraw'))
            metadata['pgo_profiles'] = [{'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in profiles]
            assert profiles, 'No PGO profiles'
            if metadata['optimization'] == 'pgo-bolt':
                bolt_profiles = list((ROOT / os.environ['CARGO_TARGET_DIR'] / 'bolt-profiles').rglob('*.fdata'))
                assert bolt_profiles, 'No BOLT profiles'
                metadata['bolt_profile_bytes'] = sum(p.stat().st_size for p in bolt_profiles)
        (OUT / 'metadata.json').write_text(json.dumps(metadata, indent=2))
elif phase == 'measure':
    execute('measure', ['python3', os.environ['RUNNER_TEMP'] + '/ci-measure.py', 'bench', '--output', str(OUT / 'benchmark'), '--binary', 'baseline=' + str(OUT / 'baseline/martin'), '--binary', 'optimized=' + str(OUT / 'optimized/martin'), '--database-url', os.environ['DATABASE_URL'], '--seconds', '10', '--repeats', '5', '--workers', '2', '--connections', '32'])
else:
    raise SystemExit('Unknown phase')
