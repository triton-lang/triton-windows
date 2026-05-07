add_triton_library(TritonInstrumentToLLVM
    InstrumentationToLLVM.cpp
    GSanToLLVM.cpp

    LINK_LIBS PUBLIC
    MLIRIR
    MLIRPass
    TritonIR
    TritonGPUIR
    TritonInstrumentIR
    TritonNvidiaGPUIR
)

# NVGPUIR is only available when the NVIDIA backend is built
if(TRITON_BUILD_NVIDIA_BACKEND)
  target_link_libraries(TritonInstrumentToLLVM PUBLIC NVGPUIR)
endif()
