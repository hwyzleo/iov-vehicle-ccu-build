# examples/minimal/CMakeLists.txt
# Minimal example for CGW BUILD v0.1-draft self-verification.
#
# Defines two targets matching manifests/services.yaml:
#   cgw-hello-lib  - shared library exporting greet()
#   cgw-hello-cli  - executable consuming cgw-hello-lib
#
# Both targets follow the CGW CMake 接入契约 (设计 §6):
#   * No compiler or sysroot overrides (toolchain/preset injects them)
#   * GNUInstallDirs for standard directories
#   * Named install components matching service manifest
#   * DESTDIR staging support

cmake_minimum_required(VERSION 3.16)
project(cgw-hello VERSION 0.1.0 LANGUAGES C CXX)

# Locate CGW CMake modules
if(DEFINED ENV{CGW_ROOT})
    set(CGW_ROOT "$ENV{CGW_ROOT}")
elseif(DEFINED CGW_ROOT)
    # cache variable from preset
else()
    get_filename_component(CGW_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../.." ABSOLUTE)
endif()
list(APPEND CMAKE_MODULE_PATH "${CGW_ROOT}/cmake/modules")

include(GNUInstallDirs)
include(CgwInstall)

# ---------------------------------------------------------------------------
# cgw-hello-lib: shared library
# ---------------------------------------------------------------------------
add_library(cgw-hello-lib SHARED
    src/greet.cpp
)
target_include_directories(cgw-hello-lib PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}/cgw>
)
set_target_properties(cgw-hello-lib PROPERTIES
    VERSION ${PROJECT_VERSION}
    SOVERSION 0
    POSITION_INDEPENDENT_CODE ON
)

# ---------------------------------------------------------------------------
# cgw-hello-cli: executable
# ---------------------------------------------------------------------------
add_executable(cgw-hello-cli
    src/hello_cli.cpp
)
target_link_libraries(cgw-hello-cli PRIVATE cgw-hello-lib)

# ---------------------------------------------------------------------------
# Install rules with components matching manifests/services.yaml
# ---------------------------------------------------------------------------
# Library: shared lib -> cgw-hello-lib-runtime
#          headers    -> cgw-hello-lib-development
cgw_install_target(cgw-hello-lib
    RUNTIME_COMPONENT cgw-hello-lib-runtime
    DEVELOPMENT_COMPONENT cgw-hello-lib-development
)
cgw_install_header(src/greet.hpp .
    COMPONENT cgw-hello-lib-development
)

# CLI: executable + systemd unit + config -> cgw-hello-cli-runtime
cgw_install_target(cgw-hello-cli
    RUNTIME_COMPONENT cgw-hello-cli-runtime
)
cgw_install_systemd_unit(systemd/cgw-hello.service
    COMPONENT cgw-hello-cli-runtime
)
cgw_install_default_config(cgw-hello-cli config/cgw-hello-cli.default.yaml
    COMPONENT cgw-hello-cli-runtime
)
