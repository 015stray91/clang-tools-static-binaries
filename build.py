#!/usr/bin/env python3
"""
build.py - Local build script for clang-tools static binaries.
Systematically hands off each completed version block right after it finishes building
to prevent losing progress if a later version fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tarfile
import urllib.request
import shutil
from pathlib import Path
from typing import Any, Literal


def _load_releases() -> dict[str, str]:
    """Load the version-to-tarball mapping from releases.json."""
    releases_path = Path(__file__).parent / "releases.json"
    if not releases_path.exists():
        # Fallback dictionary matching standard release patterns if file isn't present
        return {
            "12": "https://github.com",
            "13": "https://github.com",
            "14": "https://github.com",
            "15": "https://github.com",
            "16": "https://github.com",
            "17": "https://github.com"
        }
    with open(releases_path) as f:
        return json.load(f)


RELEASES: dict[str, str] = _load_releases()


def _load_variants() -> dict[str, Any]:
    """Load the obfuscator variant registry from ob-variants.json (if present)."""
    variants_path = Path(__file__).parent / "ob-variants.json"
    if not variants_path.exists():
        return {}
    with open(variants_path) as f:
        return json.load(f).get("variants", {})


VARIANTS: dict[str, Any] = _load_variants()

# Extra build targets for ob variants: the obfuscating compiler itself plus
# opt for -load style passes, on top of the regular tool set.
OB_EXTRA_TARGETS = ["clang", "opt"]

TOOLS = [
    "clang-format",
    "clang-query",
    "clang-tidy",
    "clang-apply-replacements",
    "clang-include-cleaner",
    "llvm-cov",
    "llvm-profdata",
    "llvm-symbolizer",
    "clang-scan-deps",
]

INCLUDE_CLEANER_MIN_VERSION = 18
CLANG_SCAN_DEPS_MIN_VERSION = 12


def active_tools(version: str) -> list[str]:
    tools = list(TOOLS)
    major = int(version.split(".")[0])
    if major < INCLUDE_CLEANER_MIN_VERSION:
        tools.remove("clang-include-cleaner")
    if major < CLANG_SCAN_DEPS_MIN_VERSION:
        tools.remove("clang-scan-deps")
    return tools


def source_info(version: str) -> tuple[str, str, Path, Path]:
    """Derive (release, url, tarball, release_dir) from releases.json.

    releases.json maps a major version to its source name
    (e.g. "12" -> "llvm-project-12.0.1.src"); the download URL, the
    tarball name, and the top source directory all derive from that
    name so they cannot drift apart.
    """
    release = RELEASES[version]
    ver_tag = release[len("llvm-project-"):-len(".src")]
    url = (
        "https://github.com/llvm/llvm-project/releases/download/"
        f"llvmorg-{ver_tag}/{release}.tar.xz"
    )
    return release, url, Path(f"{release}.tar.xz"), Path(release)


def run(cmd: list[str], **kwargs: Any) -> None:
    """Run a command, raising CalledProcessError on failure."""
    print(f"\n>>> {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def detect_os() -> str:
    system_name = platform.system().lower()
    if system_name in ("linux", "windows"):
        return system_name
    elif system_name == "darwin":
        return "macos"
    raise RuntimeError(f"Unsupported operating system: {system_name!r}")


def detect_arch() -> Literal["amd64", "arm64"]:
    machine_type = platform.machine().lower()
    return "arm64" if machine_type in ("arm64", "aarch64") else "amd64"


def sha512_file(path: Path) -> str:
    """Return the hex sha512 digest of a file."""
    h = hashlib.sha512()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download_file(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"[skip] {dest.name} already downloaded.")
        return
    print(f"Downloading {url} ...", flush=True)
    tmp = dest.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, tmp)
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def unpack_tarball(tarball: Path, release: str, extra_excludes: list[str]) -> None:
    release_dir = Path(release)
    if release_dir.exists():
        print(f"[skip] {release} already unpacked.")
        return
    print(f"Unpacking {tarball.name} ...", flush=True)
    excludes = set(extra_excludes)

    with tarfile.open(tarball, "r:xz") as tf:
        members = []
        for member in tf.getmembers():
            skip = any(member.name.startswith(excl.rstrip("*")) for excl in excludes)
            if not skip:
                members.append(member)
        tf.extractall(path=".", members=members)


# ---------------------------------------------------------------------------
# Platform-specific cmake argument builders (Using correct GCC 10 vs 12 check)
# ---------------------------------------------------------------------------

def get_linux_compiler_args(version: str) -> list[str]:
    major = int(version.split(".")[0])
    # Period-correct host compilers (newer GCC miscompiles old tblgen: sig 139).
    # Clang 12-13 -> GCC 10 (u22.04), Clang 14-17 -> GCC 11 (u22.04), 18+ -> GCC 12.
    if major < 14:
        gcc_ver = "10"
    elif major < 18:
        gcc_ver = "11"
    else:
        gcc_ver = "12"
    return [
        f"-DCMAKE_C_COMPILER=gcc-{gcc_ver}",
        f"-DCMAKE_CXX_COMPILER=g++-{gcc_ver}"
    ]


def cmake_args_linux_amd64(version: str) -> list[str]:
    args = [
        "-DBUILD_SHARED_LIBS=OFF",
        "-DLLVM_ENABLE_PROJECTS=clang;clang-tools-extra",
        "-DLLVM_BUILD_STATIC=ON",
        "-DLLVM_ENABLE_TERMINFO=OFF",
        "-DCMAKE_BUILD_TYPE=MinSizeRel",
        "-DCMAKE_CXX_FLAGS=-s -flto",
    ]
    args.extend(get_linux_compiler_args(version))
    return args


def cmake_args_linux_arm64(version: str) -> list[str]:
    args = [
        "-DBUILD_SHARED_LIBS=OFF",
        "-DLLVM_ENABLE_PROJECTS=clang;clang-tools-extra",
        "-DLLVM_BUILD_STATIC=ON",
        "-DLLVM_ENABLE_TERMINFO=OFF",
        "-DCMAKE_BUILD_TYPE=MinSizeRel",
        "-DCMAKE_CXX_FLAGS=-s -flto",
    ]
    args.extend(get_linux_compiler_args(version))
    return args


def build_variant(variant: str, platform_key: str | None, version_override: str | None) -> None:
    """Build the static tool set from an obfuscator fork.

    Version-to-version basis: the variant parallels the regular build of its
    registry base version (same static cmake recipe plus variant extras), and
    the handoff names carry the variant tag. Only kind=fork entries are
    source-buildable here; plugin/prebuilt entries need a matching LLVM
    dev sysroot or ship binaries already.
    """
    if variant not in VARIANTS:
        print(f"Error: Unknown variant {variant!r}. Registered: {', '.join(sorted(VARIANTS)) or '(none)'}")
        sys.exit(1)
    entry = VARIANTS[variant]
    kind = entry.get("kind", "fork")
    if kind != "fork":
        print(f"Error: Variant {variant!r} is kind={kind!r}, not a source fork build.")
        print(f"  note: {entry.get('note', 'no build path')}")
        sys.exit(1)
    base = entry.get("base", "tbd")
    if version_override and version_override != base:
        print(f"Error: --version {version_override} does not match {variant} base {base}.")
        sys.exit(1)
    if not base or base == "tbd":
        print(f"Error: Variant {variant!r} has no base version mapped yet (version-to-version basis required).")
        sys.exit(1)
    version = base
    if not platform_key:
        platform_key = f"{detect_os()}-{detect_arch()}"
    allowed = entry.get("platforms", ["linux-amd64", "linux-arm64"])
    if platform_key not in allowed:
        print(f"Error: Variant {variant!r} does not list platform {platform_key}. Allowed: {', '.join(allowed) or '(none)'}")
        sys.exit(1)

    # --- Step 1: Clone the fork (shallow, default branch head) ---
    src_dir = Path(f"ob-{variant}")
    if not (src_dir / ".git").exists():
        run(["git", "clone", "--depth", "1", entry["repo"], str(src_dir)])
    else:
        print(f"[skip] {src_dir} already cloned.")
    llvm_dir = src_dir / "llvm"
    if not llvm_dir.is_dir():
        print(f"Error: {src_dir} has no llvm/ source dir; expected an llvm-project-rooted fork.")
        print(f"  top level: {', '.join(sorted(p.name for p in src_dir.iterdir()))}")
        sys.exit(1)

    # --- Step 2: CMake configure (regular static recipe + variant extras) ---
    build_dir = src_dir / "build"
    build_dir.mkdir(exist_ok=True)
    if platform_key == "linux-amd64":
        cmake_flags = cmake_args_linux_amd64(version)
    elif platform_key == "linux-arm64":
        cmake_flags = cmake_args_linux_arm64(version)
    else:
        print(f"Unsupported target platform parameter: {platform_key}")
        sys.exit(1)
    cmake_flags = cmake_flags + list(entry.get("cmake_extra", []))

    # --- Step 3: Build the ob tool targets (targets only, not the world) ---
    targets = OB_EXTRA_TARGETS + active_tools(version)
    run(["cmake", "-S", str(llvm_dir), "-B", str(build_dir)] + cmake_flags)
    run(["cmake", "--build", str(build_dir), "--config", "Release", "--target"] + targets)

    # --- Step 4: Variant handoff (separate tree, variant-tagged names) ---
    staging_root = Path(os.getcwd()) / "tools" / "toolchain" / "clang-ob"
    staging_root.mkdir(parents=True, exist_ok=True)
    src_bin_dir = build_dir / "bin"
    if not src_bin_dir.exists():
        print(f"Error: Internal compiler output path missing at {src_bin_dir}")
        sys.exit(1)
    checksum_lines = []
    for tool in targets:
        src_bin = src_bin_dir / tool
        if not src_bin.exists():
            print(f"[Warning] Targeted binary artifact missing: {tool}")
            continue
        unique_name = f"{tool}-{version}_{platform_key}-{variant}"
        dest_bin = staging_root / unique_name
        shutil.copy2(src_bin, dest_bin)
        print(f"[Handoff Success] Staged: tools/toolchain/clang-ob/{unique_name}")
        checksum_lines.append(f"{sha512_file(dest_bin)}  {unique_name}\n")
    if checksum_lines:
        checksum_file = staging_root / f"SHA512SUMS-{version}_{platform_key}-{variant}"
        with open(checksum_file, "w") as cs_file:
            cs_file.writelines(checksum_lines)
        print(f"[Handoff Success] Generated signatures table at: {checksum_file.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Static Clang Tools Builder")
    parser.add_argument("--version", required=False, default=None, help="LLVM major version (e.g., 12, 17)")
    parser.add_argument("--platform", help="Target platform architecture pair")
    parser.add_argument("--variant", required=False, default=None, help="Obfuscator variant from ob-variants.json (e.g., Pluto-Obfuscator)")
    args = parser.parse_args()

    if args.variant:
        build_variant(args.variant, args.platform, args.version)
        return

    if not args.version:
        print("Error: --version is required unless --variant is given.")
        sys.exit(1)
    version = args.version
    platform_key = args.platform if args.platform else f"{detect_os()}-{detect_arch()}"

    if version not in RELEASES:
        print(f"Error: Unknown version {version!r}. Available versions: {', '.join(RELEASES.keys())}")
        sys.exit(1)

    _, url, tarball, release_dir = source_info(version)
    
    # Tarball, URL, and source dir all derive from releases.json via
    # source_info() above, so they cannot drift apart.

    # --- Step 1: Download & Extract ---
    download_file(url, tarball)
    unpack_tarball(tarball, str(release_dir), extra_excludes=[])

    # --- Step 2: Establish the CMake configurations ---
    build_dir = release_dir / "build"
    build_dir.mkdir(exist_ok=True)

    if platform_key == "linux-amd64":
        cmake_flags = cmake_args_linux_amd64(version)
    elif platform_key == "linux-arm64":
        cmake_flags = cmake_args_linux_arm64(version)
    else:
        print(f"Unsupported target platform parameter: {platform_key}")
        sys.exit(1)

    # --- Step 3: Run the Configurations & Compile ---
    run(["cmake", f"-S", str(release_dir / "llvm"), f"-B", str(build_dir)] + cmake_flags)
    # Build only the shippable tool targets (upstream parity). A bare
    # `cmake --build` builds ALL, including test tools like c-index-test
    # which needs libclang_static (not built in this config) -> ld fail.
    run(["cmake", "--build", str(build_dir), "--config", "Release",
         "--target"] + active_tools(version))

    # =========================================================================
    # SYSTEMATIC HANDOFF LOGIC
    # =========================================================================
    # Right after the build completes, pull items out of the temporary folder 
    # and stage them inside a uniform repository directory layout so the YAML 
    # can catch it instantly even if a later version execution fails.
    # =========================================================================
    staging_root = Path(os.getcwd()) / "tools" / "toolchain" / "clang"
    staging_root.mkdir(parents=True, exist_ok=True)

    src_bin_dir = build_dir / "bin"
    checksum_lines = []

    print(f"\n============================================================")
    print(f"Systematic Handoff: Relocating Validated Static Targets")
    print(f"============================================================")

    if src_bin_dir.exists():
        active_binary_set = active_tools(version)
        for tool in active_binary_set:
            src_bin = src_bin_dir / tool
            if src_bin.exists():
                # Generate unique version architecture mapping names to prevent collisions
                unique_name = f"{tool}-{version}_{platform_key}"
                dest_bin = staging_root / unique_name
                
                # Copy raw static asset directly into the persistent repository folder
                shutil.copy2(src_bin, dest_bin)
                print(f"[Handoff Success] Staged: tools/toolchain/clang/{unique_name}")
                
                # Record checksum trackers for verification pipelines
                checksum = sha512_file(dest_bin)
                checksum_lines.append(f"{checksum}  {unique_name}\n")
            else:
                print(f"[Warning] Targeted binary artifact missing: {tool}")

        # Write localized checksum tables right inside the handoff path
        if checksum_lines:
            checksum_file = staging_root / f"SHA512SUMS-{version}_{platform_key}"
            with open(checksum_file, "w") as cs_file:
                cs_file.writelines(checksum_lines)
            print(f"[Handoff Success] Generated signatures table at: {checksum_file.name}")
    else:
        print(f"Error: Internal compiler output path missing at {src_bin_dir}")
        sys.exit(1)


if __name__ == "__main__":
    main()
    
