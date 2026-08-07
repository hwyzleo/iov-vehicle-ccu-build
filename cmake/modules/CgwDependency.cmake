# CgwDependency.cmake - CGW Build 依赖查找辅助
#
# 强制目标依赖仅从受控 staging roots 查找（设计 §4）：
# 下游 SDK staging -> TARGET dependency staging -> sysroot，不得命中宿主路径。
# toolchain (cgw-target.cmake) 按此顺序填充 CMAKE_FIND_ROOT_PATH 和
# CMAKE_PREFIX_PATH，并将 find mode 设为 ONLY (library/include/package)。
#
# 必需环境（由 orchestrator 设置）：
#   CGW_DEP_STAGING  - TARGET 依赖 staging 前缀
#   CGW_SDK_STAGING  - 上游服务 SDK 前缀（构建叶子库时为空）

# cgw_find_package(<package> [REQUIRED] [COMPONENTS ...])
macro(cgw_find_package _package)
    find_package(${_package} ${ARGN})
    if(NOT ${_package}_FOUND AND NOT ${_package}_FOUND STREQUAL "")
        message(FATAL_ERROR
            "CGW dependency '${_package}' not found in target sysroot or "
            "dependency staging. Ensure it is built and installed to the "
            "staging prefix, or declared in dependency-lock.yaml. "
            "CMAKE_PREFIX_PATH=${CMAKE_PREFIX_PATH}")
    endif()
endmacro()

# cgw_find_library(<var> <library>)
macro(cgw_find_library _var _library)
    find_library(${_var} ${_library}
        REQUIRED
        NO_DEFAULT_PATH
        HINTS ${CMAKE_FIND_ROOT_PATH}
        PATH_SUFFIXES lib aarch64-linux-gnu lib/aarch64-linux-gnu
    )
endmacro()
