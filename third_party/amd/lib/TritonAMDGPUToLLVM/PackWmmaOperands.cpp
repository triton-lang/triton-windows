#include "TritonAMDGPUToLLVM/Passes.h"

#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SetVector.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Verifier.h"
#include "llvm/Pass.h"
#include "llvm/Transforms/Utils/Local.h"

#include <optional>

using namespace llvm;

#define DEBUG_TYPE "tritonamdgpu-pack-wmma-operands"

namespace {

struct LaneSource {
  Value *vector;
  unsigned index;
};

static bool isSupportedElemType(Type *type) {
  return type->isHalfTy() || type->isBFloatTy() ||
         (type->isIntegerTy() && type->getIntegerBitWidth() == 16);
}

static std::optional<unsigned> getConstantIndex(Value *value, unsigned size) {
  auto *constant = dyn_cast<ConstantInt>(value);
  if (!constant || constant->getValue().uge(size))
    return std::nullopt;
  return static_cast<unsigned>(constant->getLimitedValue(size));
}

static Value *packInsertChain(Value *root) {
  auto *dstTy = dyn_cast<FixedVectorType>(root->getType());
  auto *insert = dyn_cast<InsertElementInst>(root);
  Type *elementTy = dstTy ? dstTy->getElementType() : nullptr;
  if (!dstTy || !insert || !isSupportedElemType(elementTy) ||
      dstTy->getNumElements() != 16)
    return nullptr;

  constexpr unsigned numDst = 16;
  SmallVector<std::optional<LaneSource>, numDst> lanes(numDst);
  unsigned remaining = numDst;
  Value *cursor = root;
  // Walk backward so the first insertion seen for a lane is the value that
  // reaches the WMMA call when a chain overwrites a lane.
  while (auto *insert = dyn_cast<InsertElementInst>(cursor)) {
    auto dst = getConstantIndex(insert->getOperand(2), numDst);
    if (!dst)
      return nullptr;

    if (!lanes[*dst]) {
      auto *extract = dyn_cast<ExtractElementInst>(insert->getOperand(1));
      auto *srcTy = extract ? dyn_cast<FixedVectorType>(
                                  extract->getVectorOperand()->getType())
                            : nullptr;
      if (!extract || !srcTy ||
          srcTy->getElementType() != dstTy->getElementType() ||
          srcTy->getNumElements() != 8)
        return nullptr;
      auto srcIndex =
          getConstantIndex(extract->getOperand(1), srcTy->getNumElements());
      if (!srcIndex)
        return nullptr;
      lanes[*dst] = LaneSource{extract->getVectorOperand(), *srcIndex};
      // Every lane is defined by an insertion, so ancestors cannot affect the
      // result. This also permits optimized poison/undef vector roots.
      if (--remaining == 0)
        break;
    }
    cursor = insert->getOperand(0);
  }

  if (remaining != 0)
    return nullptr;

  SmallVector<Value *, 2> sources;
  for (const auto &lane : lanes) {
    Value *source = lane->vector;
    if (llvm::find(sources, source) == sources.end())
      sources.push_back(source);
  }
  if (sources.empty() || sources.size() > 2 ||
      sources[0]->getType() != sources.back()->getType())
    return nullptr;

  auto *srcTy = cast<FixedVectorType>(sources[0]->getType());
  SmallVector<int, numDst> mask;
  for (const auto &lane : lanes) {
    unsigned sourceNumber = lane->vector == sources[0] ? 0 : 1;
    mask.push_back(lane->index + sourceNumber * srcTy->getNumElements());
  }

  IRBuilder<> builder(insert);
  builder.SetCurrentDebugLocation(insert->getDebugLoc());
  Value *rhs = sources.size() == 2 ? sources[1] : PoisonValue::get(srcTy);
  return builder.CreateShuffleVector(sources[0], rhs, mask,
                                     root->getName() + ".packed");
}

static bool isSupportedWmmaOperand(Value *value) {
  for (auto *bitcast = dyn_cast<BitCastInst>(value); bitcast;) {
    value = bitcast->getOperand(0);
    bitcast = dyn_cast<BitCastInst>(value);
  }
  auto *vectorTy = dyn_cast<FixedVectorType>(value->getType());
  return vectorTy && vectorTy->getNumElements() == 16 &&
         isSupportedElemType(vectorTy->getElementType()) &&
         isa<InsertElementInst>(value);
}

static bool isSupportedWmma(CallInst &call) {
  Function *callee = call.getCalledFunction();
  if (!callee || !callee->getName().starts_with("llvm.amdgcn.wmma."))
    return false;
  return llvm::any_of(call.args(),
                      [](Value *arg) { return isSupportedWmmaOperand(arg); });
}

struct PackWmmaOperands : FunctionPass {
  static char ID;
  PackWmmaOperands() : FunctionPass(ID) {}

  bool runOnFunction(Function &function) override {
    SetVector<Value *> roots;
    for (BasicBlock &block : function) {
      for (Instruction &instruction : block) {
        auto *call = dyn_cast<CallInst>(&instruction);
        if (!call || !isSupportedWmma(*call))
          continue;
        for (Value *operand : call->args()) {
          Value *candidate = operand;
          while (auto *bitcast = dyn_cast<BitCastInst>(candidate))
            candidate = bitcast->getOperand(0);
          if (isa<InsertElementInst>(candidate))
            roots.insert(candidate);
        }
      }
    }

    bool changed = false;
    for (Value *root : roots) {
      Value *packed = packInsertChain(root);
      if (!packed)
        continue;
      root->replaceAllUsesWith(packed);
      RecursivelyDeleteTriviallyDeadInstructions(root);
      changed = true;
    }
    return changed;
  }
};

char PackWmmaOperands::ID = 0;

} // namespace

namespace mlir::triton::AMD {
void runPackWmmaOperandsPass(llvm::Function &function) {
  PackWmmaOperands pass;
  pass.runOnFunction(function);
  // If there are no errors, the function returns false.
  assert(!llvm::verifyFunction(function) &&
         "expected function to verify successfully");
}
} // namespace mlir::triton::AMD
