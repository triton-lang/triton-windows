"""Extract the CUDA WoA toolkit from its self-extracting installer.

The installer contains an embedded 7z archive. This script carves that archive,
extracts the toolkit components, merges them into one CUDA root, and normalizes
the ARM64 binary/library layout without running the installer or requiring
administrator privileges.

Usage: python extract_cuda.py <cuda_exe> <base_dir>
"""

import mmap
import os
import shutil
import sys

import py7zr


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("Usage: python extract_cuda.py <cuda_exe> <base_dir>")

    exe = os.path.abspath(sys.argv[1])
    base = os.path.abspath(sys.argv[2])
    payload = os.path.join(base, "cuda_payload.7z")
    extract_dir = os.path.join(base, "cuda_extract")
    root = os.path.join(base, "cuda_root")

    if not os.path.isfile(exe):
        sys.exit(f"ERROR: CUDA installer was not found at {exe}")

    os.makedirs(base, exist_ok=True)
    shutil.rmtree(extract_dir, ignore_errors=True)
    shutil.rmtree(root, ignore_errors=True)

    # Find the embedded 7z archive and carve it from the installer.
    signature = bytes([0x37, 0x7A, 0xBC, 0xAF, 0x27, 0x1C])
    with open(exe, "rb") as installer:
        with mmap.mmap(installer.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            offset = mapped.find(signature)
    if offset < 0:
        sys.exit("ERROR: 7z signature not found in CUDA installer")

    print(f"[1/4] 7z payload offset = {offset}", flush=True)
    with open(exe, "rb") as installer, open(payload, "wb") as archive:
        installer.seek(offset)
        shutil.copyfileobj(installer, archive, 1024 * 1024)
    print(f"      carved -> {payload} ({os.path.getsize(payload) / 1e9:.2f} GB)", flush=True)

    # Exclude installer metadata, documentation, Nsight, and x64 cross tools.
    skip_substrings = ("_cross_x86_64", "nsight", "documentation")
    skip_exact = {
        "ARM64",
        "CoInstaller32.exe",
        "CoInstaller64.exe",
        "CUDADevelopment",
        "CUDARuntimes",
        "CUDAToolkit",
        "EULA.txt",
        "NVI2",
        "NVMUP.cfg",
        "Setup.cfg",
        "license.txt",
        "setup.exe",
    }

    def top_level(path: str) -> str:
        return path.replace("\\", "/").split("/", 1)[0]

    with py7zr.SevenZipFile(payload, "r") as archive:
        names = archive.getnames()
        top_levels = sorted({top_level(name) for name in names})
        wanted = [
            name
            for name in top_levels
            if name not in skip_exact and not any(value in name.lower() for value in skip_substrings)
        ]
        wanted_set = set(wanted)
        targets = [name for name in names if top_level(name) in wanted_set]
        os.makedirs(extract_dir, exist_ok=True)
        print(f"[2/4] extracting {len(targets)} files from {len(wanted)} components ...", flush=True)
        archive.extract(path=extract_dir, targets=targets)
    print("      extraction complete", flush=True)

    # Each component has its own version directory; merge their toolkit trees.
    merge_directories = {"bin", "extras", "include", "lib", "lib64", "libdevice", "nvvm", "src"}
    os.makedirs(root, exist_ok=True)
    for component in os.scandir(extract_dir):
        if not component.is_dir():
            continue
        for version_dir in os.scandir(component.path):
            if not version_dir.is_dir():
                continue
            for child in os.scandir(version_dir.path):
                if child.is_dir() and child.name in merge_directories:
                    shutil.copytree(child.path, os.path.join(root, child.name), dirs_exist_ok=True)
    print(f"[3/4] merged -> {root}", flush=True)

    # Put ARM64 DLLs where Windows searches for them and provide CUDA's expected
    # x64 library alias for tools that do not yet recognize the ARM64 directory.
    bin_arm64 = os.path.join(root, "bin", "arm64")
    if os.path.isdir(bin_arm64):
        for filename in os.listdir(bin_arm64):
            if filename.lower().endswith(".dll"):
                shutil.copy2(os.path.join(bin_arm64, filename), os.path.join(root, "bin", filename))

    lib_arm64 = os.path.join(root, "lib", "arm64")
    lib_x64 = os.path.join(root, "lib", "x64")
    if os.path.isdir(lib_arm64) and not os.path.isdir(lib_x64):
        shutil.copytree(lib_arm64, lib_x64)

    ptxas = os.path.join(root, "bin", "ptxas.exe")
    print(f"[4/4] normalized. ptxas present: {os.path.exists(ptxas)}", flush=True)
    if not os.path.isfile(ptxas):
        sys.exit(f"ERROR: ptxas.exe was not found under extracted CUDA root {root}")

    # The merged root is all the workflow needs; remove intermediate copies to
    # keep enough disk space available for the multi-version wheel build.
    os.remove(payload)
    shutil.rmtree(extract_dir)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
