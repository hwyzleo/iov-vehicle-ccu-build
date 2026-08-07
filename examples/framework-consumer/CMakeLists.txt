# examples/framework-consumer/CMakeLists.txt
# CGW-BUILD-DSN-CR-002 §5: installed-package consumer self-verification.
#
# Consumes the CGW framework ONLY through the installed, relocatable CMake
# package (find_package + exported targets). It MUST NOT reference CGW-FW
# source includes, private headers, add_subdirectory or build-tree libraries.
#
# Links all five exported CGWFramework:: targets to prove they resolve
# individually and together (their yaml-cpp / pthread closure links cleanly
# via the package's find_dependency).

cmake_minimum_required(VERSION 3.16)
project(cgw-framework-consumer VERSION 0.1.0 LANGUAGES CXX)

# Locate CGW CMake modules (CGW_ROOT is injected by the orchestrator/preset).
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

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# CGW-BUILD-DSN-CR-002 §5: consume framework via installed package only.
# The orchestrator injects CMAKE_PREFIX_PATH (SDK staging -> dep staging) so
# this resolves CGWFrameworkConfig.cmake and its find_dependency(yaml-cpp).
find_package(CGWFramework CONFIG REQUIRED)

add_executable(cgw-framework-consumer src/main.cpp)

# Link all five exported targets to prove they resolve individually and together.
target_link_libraries(cgw-framework-consumer PRIVATE
        CGWFramework::cgw-framework-config
        CGWFramework::cgw-framework-log
        CGWFramework::cgw-framework-store
        CGWFramework::cgw-framework-hash
        CGWFramework::cgw-framework-someip
        )

# Install rules with component matching manifests/services.yaml.
# Executable + systemd unit + config -> cgw-framework-consumer-runtime.
cgw_install_target(cgw-framework-consumer
        RUNTIME_COMPONENT cgw-framework-consumer-runtime
)
cgw_install_systemd_unit(systemd/cgw-framework-consumer.service
        COMPONENT cgw-framework-consumer-runtime
)
cgw_install_default_config(framework-consumer config/framework-consumer.default.yaml
        COMPONENT cgw-framework-consumer-runtime
)
