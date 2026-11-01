# examples/minimal/CMakeLists.txt
# Minimal example for CCU BUILD v0.1-draft self-verification.
#
# Defines two targets matching manifests/services.yaml:
#   ccu-hello-lib  - shared library exporting greet()
#   ccu-hello-cli  - executable consuming ccu-hello-lib
#
# Both targets follow the CCU CMake 接入契约 (设计 §6):
#   * No compiler or sysroot overrides (toolchain/preset injects them)
#   * GNUInstallDirs for standard directories
#   * Named install components matching service manifest
#   * DESTDIR staging support

cmake_minimum_required(VERSION 3.16)
project(ccu-hello VERSION 0.1.0 LANGUAGES C CXX)

# Locate CCU CMake modules
if(DEFINED ENV{CCU_ROOT})
    set(CCU_ROOT "$ENV{CCU_ROOT}")
elseif(DEFINED CCU_ROOT)
    # cache variable from preset
else()
    get_filename_component(CCU_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
endif()
list(APPEND CMAKE_MODULE_PATH "${CCU_ROOT}/cmake/modules")

include(GNUInstallDirs)
include(CcuInstall)

# ---------------------------------------------------------------------------
# ccu-hello-lib: shared library
# ---------------------------------------------------------------------------
add_library(ccu-hello-lib SHARED
    src/greet.cpp
)
target_include_directories(ccu-hello-lib PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}/ccu>
)
set_target_properties(ccu-hello-lib PROPERTIES
    VERSION ${PROJECT_VERSION}
    SOVERSION 0
    POSITION_INDEPENDENT_CODE ON
)

# ---------------------------------------------------------------------------
# ccu-hello-cli: executable
# ---------------------------------------------------------------------------
add_executable(ccu-hello-cli
    src/hello_cli.cpp
)
target_link_libraries(ccu-hello-cli PRIVATE ccu-hello-lib)

# ---------------------------------------------------------------------------
# Install rules with components matching manifests/services.yaml
# ---------------------------------------------------------------------------
# Library: shared lib -> ccu-hello-lib-runtime
#          headers    -> ccu-hello-lib-development
ccu_install_target(ccu-hello-lib
    RUNTIME_COMPONENT ccu-hello-lib-runtime
    DEVELOPMENT_COMPONENT ccu-hello-lib-development
)
ccu_install_header(src/greet.hpp .
    COMPONENT ccu-hello-lib-development
)

# CLI: executable + systemd unit + config -> ccu-hello-cli-runtime
ccu_install_target(ccu-hello-cli
    RUNTIME_COMPONENT ccu-hello-cli-runtime
)
ccu_install_systemd_unit(systemd/ccu-hello.service
    COMPONENT ccu-hello-cli-runtime
)
ccu_install_default_config(ccu-hello-cli config/ccu-hello-cli.default.yaml
    COMPONENT ccu-hello-cli-runtime
)
