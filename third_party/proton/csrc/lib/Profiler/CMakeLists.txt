set(PROTON_PROFILER_SOURCES
  Profiler.cpp
  GPUProfiler.cpp
  Graph.cpp
  Cupti/CuptiPCSampling.cpp
  Cupti/CuptiProfiler.cpp
  RocTracer/RoctracerProfiler.cpp
  RocprofSDK/RocprofSDKProfiler.cpp
  Instrumentation/InstrumentationProfiler.cpp
  Instrumentation/Metadata.cpp
)
if(NOT WIN32)
  list(APPEND PROTON_PROFILER_SOURCES
    RocprofSDK/RocprofSDKPCSampling.cpp
    RocprofSDK/RocprofSDKPCSamplingSourceLocations.cpp
  )
endif()

add_proton_library(ProtonProfiler ${PROTON_PROFILER_SOURCES})
