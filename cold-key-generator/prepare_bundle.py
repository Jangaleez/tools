#!/usr/bin/env python3
"""Cold Key Generator - bundle maker. Run this on your normal, ONLINE computer (Windows or Linux).

It downloads the official tools, checks them, and puts everything the offline generator needs into
one folder - ideally straight onto a USB stick:

  - Bitcoin Core (from bitcoincore.org) + its signed checksum file + the builders' public keys
  - Solana CLI (from the official anza-xyz/agave GitHub release), checked against GitHub's recorded hash
  - The BIP39 reference implementation and the Ethereum Foundation's eth-account library (from PyPI,
    pinned by hash), plus the official BIP39 test vectors
  - The generator itself, and a MANIFEST of every file's hash

Usage:
    python prepare_bundle.py E:\\coldkeys          (Windows - E: being your USB stick)
    python3 prepare_bundle.py /media/you/USB/coldkeys   (Linux)
"""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "bundle-files"
sys.path.insert(0, str(SRC))
import coldkeys as ck  # noqa: E402  (shares the pinned version numbers and hashes)

SOLANA_TAG = "v4.4.1"
BTC_BASE = f"https://bitcoincore.org/bin/bitcoin-core-{ck.BTC_VERSION}"
GUIX_KEYS = "https://raw.githubusercontent.com/bitcoin-core/guix.sigs/main/builder-keys"
BIP39_VECTORS = "https://raw.githubusercontent.com/trezor/python-mnemonic/master/vectors.json"
UA = {"User-Agent": "cold-key-generator-prepare/1.0"}


def get(url, dest=None):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        if dest is None:
            return r.read()
        with open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r    {done / 1e6:6.1f} / {total / 1e6:.1f} MB", end="", flush=True)
        if total:
            print()
    return dest


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def step(text):
    print(f"\n== {text}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", help="Folder to create (e.g. on your USB stick)")
    ap.add_argument("--python-version", default="3.13",
                    help="Python version on the offline computer (Tails 7 = 3.13)")
    ap.add_argument("--solana-tag", default=SOLANA_TAG, help=f"Solana CLI release (default {SOLANA_TAG})")
    args = ap.parse_args()

    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} already exists and isn't empty. Choose a new folder.")
    (out / "bitcoin" / "builder-keys").mkdir(parents=True)
    (out / "solana").mkdir()
    (out / "python").mkdir()

    # ------------------------------------------------ Bitcoin Core
    step(f"Bitcoin Core {ck.BTC_VERSION} (bitcoincore.org)")
    tarball = out / "bitcoin" / ck.BTC_TARBALL
    get(f"{BTC_BASE}/{ck.BTC_TARBALL}", tarball)
    get(f"{BTC_BASE}/SHA256SUMS", out / "bitcoin" / "SHA256SUMS")
    get(f"{BTC_BASE}/SHA256SUMS.asc", out / "bitcoin" / "SHA256SUMS.asc")
    digest = sha256(tarball)
    if digest != ck.BTC_SHA256:
        sys.exit(f"STOP: the download's hash {digest} isn't the one Bitcoin Core's builders signed.")
    if f"{digest}  {ck.BTC_TARBALL}" not in (out / "bitcoin" / "SHA256SUMS").read_text():
        sys.exit("STOP: SHA256SUMS doesn't list this download.")
    print("    Hash matches the one signed by 13 Bitcoin Core builders.")
    for name in sorted(ck.BUILDERS):
        get(f"{GUIX_KEYS}/{name}.gpg", out / "bitcoin" / "builder-keys" / f"{name}.gpg")
    print(f"    {len(ck.BUILDERS)} builder keys saved (signatures are checked on the offline computer).")

    # ------------------------------------------------ Solana CLI
    step(f"Solana CLI {args.solana_tag} (github.com/anza-xyz/agave)")
    rel = json.loads(get(f"https://api.github.com/repos/anza-xyz/agave/releases/tags/{args.solana_tag}"))
    asset = next((a for a in rel.get("assets", []) if a["name"] == ck.SOL_TARBALL), None)
    if not asset:
        sys.exit(f"STOP: {ck.SOL_TARBALL} isn't in release {args.solana_tag}.")
    expected = (asset.get("digest") or "").removeprefix("sha256:")
    if not expected:
        sys.exit("STOP: GitHub has no recorded hash for this file, so it can't be checked.")
    sol = out / "solana" / ck.SOL_TARBALL
    get(asset["browser_download_url"], sol)
    if sha256(sol) != expected:
        sys.exit("STOP: the Solana download doesn't match GitHub's recorded hash.")
    (out / "solana" / "release.json").write_text(json.dumps(
        {"tag": args.solana_tag, "file": ck.SOL_TARBALL, "sha256": expected,
         "source": asset["browser_download_url"], "published": rel.get("published_at")}, indent=2))
    print("    Matches GitHub's recorded hash for the official release.")

    # ------------------------------------------------ Python libraries
    step(f"Python libraries for Python {args.python_version} (pypi.org, pinned by hash)")
    pyver = args.python_version.replace(".", "")
    lock = HERE / f"requirements-py{pyver}.lock"
    if not lock.exists():
        sys.exit(f"STOP: no pinned library list for Python {args.python_version} ({lock.name}).")
    cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--require-hashes",
           "--only-binary=:all:", "--implementation", "cp", "--python-version", args.python_version,
           "--abi", f"cp{pyver}", "--abi", "abi3", "--abi", "none",
           "--platform", "manylinux_2_28_x86_64", "--platform", "manylinux_2_17_x86_64",
           "--platform", "manylinux2014_x86_64", "--platform", "any",
           "-r", str(lock), "-d", str(out / "python")]
    if subprocess.run(cmd).returncode != 0:
        sys.exit("STOP: pip couldn't download the libraries (or a hash didn't match).")
    get(BIP39_VECTORS, out / "python" / "bip39-vectors.json")
    if sha256(out / "python" / "bip39-vectors.json") != ck.BIP39_VECTORS_SHA256:
        sys.exit("STOP: the BIP39 test vectors file isn't the expected official one.")
    print(f"    {len(list((out / 'python').glob('*.whl')))} libraries + official BIP39 test vectors.")

    # ------------------------------------------------ generator + manifest
    step("Generator and manifest")
    for f in ("coldkeys.py", "start.sh", "README-OFFLINE.txt"):
        shutil.copy2(SRC / f, out / f)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    manifest = "".join(f"{sha256(p)}  {p.relative_to(out).as_posix()}\n" for p in files)
    (out / "MANIFEST.sha256").write_text(manifest, newline="\n")
    code = hashlib.sha256(manifest.encode()).hexdigest()[:16].upper()
    size = sum(p.stat().st_size for p in files) / 1e6
    print(f"    {len(files)} files, {size:.0f} MB.")

    print(f"\nDone: {out}")
    print(f"\n  Bundle code:  {' '.join(code[i:i + 4] for i in range(0, 16, 4))}")
    print("\nWrite the bundle code down. The offline generator shows it too - if they differ, "
          "the bundle was changed after this point. Don't use it.")


if __name__ == "__main__":
    if sys.version_info < (3, 10):
        sys.exit("Needs Python 3.10 or newer.")
    main()
