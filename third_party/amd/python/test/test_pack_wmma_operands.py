import re

import pytest
import triton
import triton.language as tl
from triton._C.libtriton import amd
from triton.compiler import ASTSource

current_target = triton.runtime.driver.active.get_current_target()
if not current_target.arch.startswith(("gfx11", "gfx12")):
    pytest.skip("WMMA operand packing only applies to gfx11/gfx12", allow_module_level=True)


@triton.jit
def pack_wmma_operands_kernel(a_ptr, b_ptr, c_ptr, n):
    m = tl.arange(0, 16)
    k = tl.arange(0, 16)
    a = tl.load(a_ptr + m[:, None] * 16 + k[None, :])
    acc = tl.zeros((16, 16), dtype=tl.float32)
    for i in tl.range(0, n, num_stages=3, loop_unroll_factor=1):
        b = tl.load(b_ptr + i * 256 + m[:, None] * 16 + k[None, :])
        acc += tl.dot(a, b)
    tl.store(c_ptr + m[:, None] * 16 + m[None, :], acc)


def _resource_usage(asm: str, key: str) -> int:
    match = re.search(rf"(?m)^\s*\.{key}:\s*(\d+)\s*$", asm)
    assert match is not None, f"missing .{key} in amdgcn output"
    return int(match.group(1))


@pytest.mark.parametrize("dtype, llvm_type", [("bf16", "bfloat"), ("fp16", "half")])
def test_pack_wmma_operands(dtype: str, llvm_type: str, monkeypatch: pytest.MonkeyPatch) -> None:
    source = ASTSource(
        fn=pack_wmma_operands_kernel,
        signature={"a_ptr": f"*{dtype}", "b_ptr": f"*{dtype}", "c_ptr": "*fp32", "n": "i32"},
    )
    options = {"num_warps": 1}
    monkeypatch.setattr(triton.knobs.compilation, "always_compile", True)
    packed = triton.compile(source, target=current_target, options=options)

    monkeypatch.setattr(amd, "add_pack_wmma_operands_llvm_pass", lambda fn: None)
    unpacked = triton.compile(source, target=current_target, options=options)

    packed_llir = packed.asm["llir"]
    unpacked_llir = unpacked.asm["llir"]
    chain = f"insertelement <16 x {llvm_type}>"
    assert unpacked_llir.count(chain) > 0
    assert packed_llir.count(chain) == 0
    assert f"shufflevector <8 x {llvm_type}>" in packed_llir
    assert "tail call <8 x float> @llvm.amdgcn.wmma" in packed_llir

    packed_asm = packed.asm["amdgcn"]
    unpacked_asm = unpacked.asm["amdgcn"]
    vgpr_packed = _resource_usage(packed_asm, "vgpr_count")
    vgpr_unpacked = _resource_usage(unpacked_asm, "vgpr_count")
    assert vgpr_packed == 45
    assert vgpr_unpacked == 53
