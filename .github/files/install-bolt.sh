#!/usr/bin/env bash
# Run as root on Ubuntu. Keep BOLT's LLVM major aligned with the Rust compiler.
set -euo pipefail
llvm_major=${1:-23}
case "$llvm_major" in
    ''|*[!0-9]*) echo 'Expected a numeric LLVM major version' >&2; exit 1 ;;
esac
# shellcheck source=/dev/null
source /etc/os-release
[[ "$ID" == ubuntu ]] || { echo 'This installer requires Ubuntu' >&2; exit 1; }

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl
mkdir -p /etc/apt/keyrings
curl --fail --silent --show-error --retry 3 https://apt.llvm.org/llvm-snapshot.gpg.key \
    --output /etc/apt/keyrings/martin-llvm.asc
printf 'deb [signed-by=/etc/apt/keyrings/martin-llvm.asc] https://apt.llvm.org/%s/ llvm-toolchain-%s-%s main\n' \
    "$VERSION_CODENAME" "$VERSION_CODENAME" "$llvm_major" > /etc/apt/sources.list.d/martin-llvm.list
apt-get update
apt-get install -y --no-install-recommends "bolt-$llvm_major"
ln -sfn "/usr/bin/llvm-bolt-$llvm_major" /usr/local/bin/llvm-bolt
ln -sfn "/usr/bin/merge-fdata-$llvm_major" /usr/local/bin/merge-fdata
ln -sfn "/usr/lib/llvm-$llvm_major/lib/libbolt_rt_instr.a" /usr/local/lib/libbolt_rt_instr.a
llvm-bolt --version
