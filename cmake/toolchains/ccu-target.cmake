# ccu-target.cmake - CCU Build 平台参数化 toolchain
#
# CR: CCU-BUILD-DSN-CR-001 §4 Toolchain 与查找隔离
#
# 被 CMake 在使用 <platform>-debug/release preset 或 orchestrator 传入
# -DCMAKE_TOOLCHAIN_FILE=<本文件> 时加载。
#
# 项目 MUST NOT 在自身 CMakeLists.txt 中设置 compiler/sysroot。
# 所有目标环境决策由本文件和 Platform Manifest 决定。
#
# 必需变量（通过 -D 或环境变量设置）：
#   CCU_SYSROOT       - 受控 sysroot 路径
#   CCU_DEP_STAGING   - (可选) TARGET 依赖 staging 前缀
#   CCU_SDK_STAGING   - (可选) 上游服务 SDK staging 前缀
#   CCU_CROSS_CC      - (可选) C 交叉编译器，默认从平台清单读取
#   CCU_CROSS_CXX     - (可选) C++ 交叉编译器
#
# 查找顺序（设计 §4）：当前服务所需 SDK staging -> TARGET dependency
# staging -> sysroot。PROGRAM 使用 HOST 边界；LIBRARY/INCLUDE/PACKAGE
# 只允许 TARGET roots。

# --- 目标系统标识 ---
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR "$ENV{CCU_TARGET_ARCH}")

# --- 编译器 ---
# 从环境变量或 cache 变量读取交叉编译器。
if(DEFINED ENV{CCU_CROSS_CC})
    set(CMAKE_C_COMPILER "$ENV{CCU_CROSS_CC}")
elseif(DEFINED CCU_CROSS_CC)
    set(CMAKE_C_COMPILER "${CCU_CROSS_CC}")
else()
    set(CMAKE_C_COMPILER gcc)
endif()

if(DEFINED ENV{CCU_CROSS_CXX})
    set(CMAKE_CXX_COMPILER "$ENV{CCU_CROSS_CXX}")
elseif(DEFINED CCU_CROSS_CXX)
    set(CMAKE_CXX_COMPILER "${CCU_CROSS_CXX}")
else()
    set(CMAKE_CXX_COMPILER g++)
endif()

# --- Sysroot ---
if(DEFINED ENV{CCU_SYSROOT})
    set(_ccu_sysroot "$ENV{CCU_SYSROOT}")
elseif(DEFINED CCU_SYSROOT)
    set(_ccu_sysroot "${CCU_SYSROOT}")
else()
    message(FATAL_ERROR
        "CCU_SYSROOT is not set. Pass -DCCU_SYSROOT=<path> or set the "
        "CCU_SYSROOT environment variable to the sysroot path.")
endif()

if(NOT IS_DIRECTORY "${_ccu_sysroot}")
    message(FATAL_ERROR "CCU_SYSROOT does not exist or is not a directory: ${_ccu_sysroot}")
endif()

set(CMAKE_SYSROOT "${_ccu_sysroot}")

# --- Find root path ---
# 查找顺序（设计 §4）：SDK staging -> TARGET dependency staging -> sysroot。
# 每个仅在定义且非空时前置，避免空值塌缩为 "/usr"（宿主污染）。
#
# 重要：find-root 条目必须是包含 /usr 的 DESTDIR *根*，即
# ${CCU_*_STAGING}，而非 ${CCU_*_STAGING}/usr。
set(CMAKE_FIND_ROOT_PATH "${CMAKE_SYSROOT}")
if(DEFINED ENV{CCU_SDK_STAGING} AND NOT "$ENV{CCU_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "$ENV{CCU_SDK_STAGING}")
elseif(DEFINED CCU_SDK_STAGING AND NOT "${CCU_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "${CCU_SDK_STAGING}")
endif()
if(DEFINED ENV{CCU_DEP_STAGING} AND NOT "$ENV{CCU_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "$ENV{CCU_DEP_STAGING}")
elseif(DEFINED CCU_DEP_STAGING AND NOT "${CCU_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_FIND_ROOT_PATH "${CCU_DEP_STAGING}")
endif()

# CMAKE_PREFIX_PATH 镜像 find-root 顺序
set(CMAKE_PREFIX_PATH "")
if(DEFINED ENV{CCU_SDK_STAGING} AND NOT "$ENV{CCU_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "$ENV{CCU_SDK_STAGING}/usr")
elseif(DEFINED CCU_SDK_STAGING AND NOT "${CCU_SDK_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "${CCU_SDK_STAGING}/usr")
endif()
if(DEFINED ENV{CCU_DEP_STAGING} AND NOT "$ENV{CCU_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "$ENV{CCU_DEP_STAGING}/usr")
elseif(DEFINED CCU_DEP_STAGING AND NOT "${CCU_DEP_STAGING}" STREQUAL "")
    list(PREPEND CMAKE_PREFIX_PATH "${CCU_DEP_STAGING}/usr")
endif()

# --- 多级 SDK staging ---
# CCU_SDK_STAGING_DIRS 是 ':' 分隔的路径列表，用于注入多个上游
# service dependency SDK。遍历每个 <dir>/usr，按声明顺序加入查找路径。
if(DEFINED ENV{CCU_SDK_STAGING_DIRS} AND NOT "$ENV{CCU_SDK_STAGING_DIRS}" STREQUAL "")
    string(REPLACE ":" ";" _ccu_sdk_dirs "$ENV{CCU_SDK_STAGING_DIRS}")
elseif(DEFINED CCU_SDK_STAGING_DIRS AND NOT "${CCU_SDK_STAGING_DIRS}" STREQUAL "")
    set(_ccu_sdk_dirs "${CCU_SDK_STAGING_DIRS}")
else()
    set(_ccu_sdk_dirs "")
endif()
foreach(_ccu_sdk_root IN LISTS _ccu_sdk_dirs)
    if(IS_DIRECTORY "${_ccu_sdk_root}/usr")
        list(PREPEND CMAKE_FIND_ROOT_PATH "${_ccu_sdk_root}")
        list(PREPEND CMAKE_PREFIX_PATH "${_ccu_sdk_root}/usr")
    endif()
endforeach()
unset(_ccu_sdk_dirs)
unset(_ccu_sdk_root)

# --- Find mode ---
# PROGRAM (generator/code generator): 仅搜索 HOST 路径。
# LIBRARY/INCLUDE/PACKAGE: 仅搜索 TARGET (sysroot/staging)。
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)

# --- C/C++ 标准 ---
set(CMAKE_C_STANDARD 11 CACHE STRING "C standard")
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17 CACHE STRING "C++ standard")
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

# --- ABI: 使用编译器默认 libstdc++ C++11 ABI ---
# 项目 MUST NOT 覆盖 _GLIBCXX_USE_CXX11_ABI。

# --- 安全和诊断编译选项（默认） ---
set(CCU_WARN_FLAGS "-Wall -Wextra -Wformat -Wformat-security")
set(CCU_SECURITY_FLAGS "-fstack-protector-strong -D_FORTIFY_SOURCE=2")
set(CCU_TARGET_C_FLAGS_INIT "${CCU_WARN_FLAGS} ${CCU_SECURITY_FLAGS}")
set(CCU_TARGET_CXX_FLAGS_INIT "${CCU_WARN_FLAGS} ${CCU_SECURITY_FLAGS}")

if(NOT DEFINED CMAKE_C_FLAGS_INIT)
    set(CMAKE_C_FLAGS_INIT "${CCU_TARGET_C_FLAGS_INIT}")
endif()
if(NOT DEFINED CMAKE_CXX_FLAGS_INIT)
    set(CMAKE_CXX_FLAGS_INIT "${CCU_TARGET_CXX_FLAGS_INIT}")
endif()

# --- RPATH 策略 ---
# 构建树 RPATH 在开发期间允许，但必须从 release 制品中剥离。
# orchestrator 运行 elfcheck 强制执行。Release: 最终制品无 RPATH。
# $ORIGIN 相对 RPATH 对库允许。
if(NOT DEFINED CMAKE_INSTALL_RPATH)
    set(CMAKE_INSTALL_RPATH "")
endif()
if(NOT DEFINED CMAKE_BUILD_RPATH_USE_ORIGIN)
    set(CMAKE_BUILD_RPATH_USE_ORIGIN TRUE)
endif()

# --- 诊断信息（configure 输出可见） ---
message(STATUS "CCU target toolchain:")
message(STATUS "  C compiler:       ${CMAKE_C_COMPILER}")
message(STATUS "  C++ compiler:     ${CMAKE_CXX_COMPILER}")
message(STATUS "  Sysroot:          ${CMAKE_SYSROOT}")
message(STATUS "  Find root path:   ${CMAKE_FIND_ROOT_PATH}")
message(STATUS "  Prefix path:      ${CMAKE_PREFIX_PATH}")
message(STATUS "  C standard:       ${CMAKE_C_STANDARD}")
message(STATUS "  C++ standard:     ${CMAKE_CXX_STANDARD}")
