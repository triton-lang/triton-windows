from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import pytest
import tarfile
import tempfile

from pathlib import Path

import triton

from triton.runtime.build import compile_module_from_src

TEST_MODULE_C = """
#include <Python.h>
#include <string.h>

static PyObject* go(PyObject* self, PyObject* args) {
    const char *command;
    if (!PyArg_ParseTuple(args, "s", &command))
        return NULL;

    const char* res;
    if (strcmp(command, "hello") == 0) {
        res = "hiya";
    } else {
        res = "huh";
    }
    return PyUnicode_FromString(res);
}

static PyMethodDef ModuleMethods[] = {
  {"go", go, METH_VARARGS, "test_module.go for testing"},
  {NULL, NULL, 0, NULL}
};

static struct PyModuleDef ModuleDef = {
  PyModuleDef_HEAD_INIT,
  "test_module",
  NULL, //documentation
  -1, //size
  ModuleMethods
};

PyMODINIT_FUNC PyInit_test_module(void) {
  PyObject *m = PyModule_Create(&ModuleDef);
  if(m == NULL) {
    return NULL;
  }
  PyModule_AddFunctions(m, ModuleMethods);
  return m;
}
"""


def test_compile_module(fresh_triton_cache):
    mod = compile_module_from_src(TEST_MODULE_C, "test_module")

    with pytest.raises(Exception):
        mod.go()

    assert mod.go("huh") == "huh"
    assert mod.go("hello") == "hiya"

    # Make sure the module is cached
    mod2 = compile_module_from_src(TEST_MODULE_C, "test_module")
    assert mod2.__file__ == mod.__file__


def test_compile_module_bad_cache(fresh_knobs):
    with tempfile.TemporaryDirectory() as tmpd:
        tmp = Path(tmpd)
        called_get_file = False

        class InvalidFileCacheManager(triton.runtime.cache.FileCacheManager):

            def get_file(self, filename: str) -> str | None:
                nonlocal called_get_file
                called_get_file = True
                (tmp / filename).write_text("not an so")
                return str(tmp / filename)

        # First corrupt the cache
        fresh_knobs.cache.manager_class = InvalidFileCacheManager

        mod = compile_module_from_src(TEST_MODULE_C, "test_module")
        assert called_get_file

        with pytest.raises(Exception):
            mod.go()

        assert mod.go("huh") == "huh"
        assert mod.go("hello") == "hiya"


@pytest.mark.parametrize("artifact", ["bootstrap", "codegen"])
@pytest.mark.parametrize(
    "mirror", ["", "https://mirror.example/generic/oaiartifacts", "https://mirror.example/generic/oaiartifacts/"])
def test_amd_codegen_download_mirror(artifact, mirror, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3]))
    import build_helpers

    monkeypatch.setenv("OAIARTIFACTS_BASE_URL", mirror)
    monkeypatch.delenv("TRITON_CACHE_DEPENDENCY_DOWNLOADS", raising=False)
    monkeypatch.setattr(build_helpers, "get_base_dir", lambda: str(tmp_path))
    parser = argparse.ArgumentParser()
    build_helpers.add_common_args(parser)
    args = build_helpers.normalize_parsed_args(
        parser.parse_args([
            "--triton-cache-path",
            str(tmp_path / "cache"),
            "--triton-llvm-system-suffix",
            "ubuntu-arm64",
            "--llvm-syspath",
            str(tmp_path / "unrelated-core-llvm"),
        ]))

    revision = "123456789abcdef"
    name = f"{'llvm' if artifact == 'bootstrap' else 'amd-codegen'}-{revision[:8]}-ubuntu-arm64-1"
    relative_path = "lib/cmake/llvm/LLVMConfig.cmake" if artifact == "bootstrap" else (
        f"lib/{build_helpers.amd_codegen_library_name()}")
    contents = b"test artifact"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(f"{name}/{relative_path}")
        member.size = len(contents)
        archive.addfile(member, io.BytesIO(contents))
    archive_bytes = buffer.getvalue()
    checksum = hashlib.sha256(archive_bytes).hexdigest()
    llvm_info = {"llvm_hash": revision, "build_number": 1, "sha256sum": {"ubuntu-arm64": checksum}}
    if artifact == "bootstrap":
        llvm_info["bootstrap_llvm"] = {"build_number": 1, "sha256sum": llvm_info.pop("sha256sum")}

    downloads = []

    def download(url, path, label):
        downloads.append(url)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(archive_bytes)

    monkeypatch.setattr(build_helpers, "_download_file", download)
    if artifact == "bootstrap":
        result = Path(build_helpers.download_codegen_llvm("amd", llvm_info, args)) / relative_path
    else:
        (tmp_path / "cmake").mkdir()
        (tmp_path / "cmake" / "amd-llvm-info.json").write_text(json.dumps(llvm_info))
        build_helpers.download_and_copy_amd_codegen(args)
        result = tmp_path / "third_party" / "amd" / "backend" / relative_path

    base = "https://mirror.example/generic/triton-llvm" if mirror else (
        "https://oaitriton.blob.core.windows.net/public/llvm-builds")
    assert downloads == [f"{base}/{name}.tar.gz"]
    assert result.read_bytes() == contents


def test_find_rocm_clang_cl_uses_package_dirs(monkeypatch, tmp_path):
    from triton.runtime import build

    missing = tmp_path / "missing"
    package = tmp_path / "site-packages" / "_rocm_sdk_core"
    clang_cl = package / "lib" / "llvm" / "bin" / "clang-cl.exe"
    clang_cl.parent.mkdir(parents=True)
    clang_cl.write_bytes(b"")
    monkeypatch.setattr(build, "find_package_paths", lambda name: [missing, package])

    assert build._find_rocm_clang_cl() == str(clang_cl)


def test_find_tcc_uses_package_dir(monkeypatch, tmp_path):
    from triton.runtime import build

    tcc = tmp_path / "tcc" / "tcc.exe"
    tcc.parent.mkdir()
    tcc.write_bytes(b"")
    monkeypatch.setattr(build, "__file__", str(tmp_path / "build.py"))

    assert build._find_tcc() == str(tcc)


def _make_bundled_cuda_tree(root: Path) -> Path:
    from triton.windows_utils import _arch_candidates

    arch = _arch_candidates()[0]
    (root / "cuda_nvcc" / "bin").mkdir(parents=True)
    (root / "cuda_nvcc" / "bin" / "ptxas.exe").write_bytes(b"")
    (root / "cuda_runtime" / "include").mkdir(parents=True)
    (root / "cuda_runtime" / "include" / "cuda.h").write_bytes(b"")
    (root / "cuda_runtime" / "lib" / arch).mkdir(parents=True)
    (root / "cuda_runtime" / "lib" / arch / "cuda.lib").write_bytes(b"")
    return root


@pytest.mark.skipif(os.name != "nt", reason="windows_utils is Windows-only")
def test_find_cuda_bundled_uses_package_dir(monkeypatch, tmp_path):
    from triton import windows_utils

    nvidia = _make_bundled_cuda_tree(tmp_path / "backends" / "nvidia")
    monkeypatch.setattr(windows_utils, "__file__", str(tmp_path / "windows_utils.py"))

    bin_path, inc_dirs, lib_dirs = windows_utils.find_cuda_bundled()
    assert bin_path == str(nvidia / "cuda_nvcc" / "bin")
    assert inc_dirs == [str(nvidia / "cuda_runtime" / "include")]
    assert lib_dirs == [str(nvidia / "cuda_runtime" / "lib" / windows_utils._arch_candidates()[0])]


@pytest.mark.skipif(os.name != "nt", reason="windows_utils is Windows-only")
def test_find_cuda_pip_uses_package_dirs(monkeypatch, tmp_path):
    from triton import windows_utils

    incomplete = tmp_path / "first" / "nvidia"
    (incomplete / "cuda_nvcc" / "bin").mkdir(parents=True)
    complete = _make_bundled_cuda_tree(tmp_path / "second" / "nvidia")
    monkeypatch.setattr(windows_utils, "find_package_paths", lambda name: [incomplete, complete])

    bin_path, inc_dirs, lib_dirs = windows_utils.find_cuda_pip()
    assert bin_path == str(complete / "cuda_nvcc" / "bin")
    assert inc_dirs == [str(complete / "cuda_runtime" / "include")]
    assert lib_dirs == [str(complete / "cuda_runtime" / "lib" / windows_utils._arch_candidates()[0])]
