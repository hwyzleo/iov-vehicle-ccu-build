# examples/framework-consumer/CMakeLists.txt
# CCU-BUILD-DSN-CR-002 §5: installed-package consumer self-verification.
#
# Consumes the CCU framework ONLY through the installed, relocatable CMake
# package (find_package + exported targets). It MUST NOT reference CCU-FW
# source includes, private headers, add_subdirectory or build-tree libraries.
#
# Links all five exported CCUFramework:: targets to prove they resolve
# individually and together (their yaml-cpp / pthread closure links cleanly
# via the package's find_dependency).

cmake_minimum_required(VERSION 3.16)
project(ccu-framework-consumer VERSION 0.1.0 LANGUAGES CXX)

# Locate CCU CMake modules (CCU_ROOT is injected by the orchestrator/preset).
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

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# CCU-BUILD-DSN-CR-002 §5: consume framework via installed package only.
# The orchestrator injects CMAKE_PREFIX_PATH (SDK staging -> dep staging) so
# this resolves CCUFrameworkConfig.cmake and its find_dependency(yaml-cpp).
find_package(CCUFramework CONFIG REQUIRED)

add_executable(ccu-framework-consumer src/main.cpp)

# Link all five exported targets to prove they resolve individually and together.
target_link_libraries(ccu-framework-consumer PRIVATE
        CCUFramework::ccu-framework-config
        CCUFramework::ccu-framework-log
        CCUFramework::ccu-framework-store
        CCUFramework::ccu-framework-hash
        CCUFramework::ccu-framework-someip
        )

# Install rules with component matching manifests/services.yaml.
# Executable + systemd unit + config -> ccu-framework-consumer-runtime.
ccu_install_target(ccu-framework-consumer
        RUNTIME_COMPONENT ccu-framework-consumer-runtime
)
ccu_install_systemd_unit(systemd/ccu-framework-consumer.service
        COMPONENT ccu-framework-consumer-runtime
)
ccu_install_default_config(framework-consumer config/framework-consumer.default.yaml
        COMPONENT ccu-framework-consumer-runtime
)
