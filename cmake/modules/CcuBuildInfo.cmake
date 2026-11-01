# CcuBuildInfo.cmake - CCU Build 构建信息生成
#
# 生成包含 git commit、构建 profile 和时间戳的构建信息头文件，
# 用于制品追溯。

# ccu_generate_build_info(<output-dir>)
function(ccu_generate_build_info _output_dir)
    execute_process(
        COMMAND git rev-parse HEAD
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
        OUTPUT_VARIABLE _ccu_git_commit
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(NOT _ccu_git_commit)
        set(_ccu_git_commit "unknown")
    endif()

    execute_process(
        COMMAND git status --porcelain
        WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
        OUTPUT_VARIABLE _ccu_git_status
        OUTPUT_STRIP_TRAILING_WHITESPACE
        ERROR_QUIET
    )
    if(_ccu_git_status)
        set(_ccu_git_dirty 1)
    else()
        set(_ccu_git_dirty 0)
    endif()

    string(TIMESTAMP _ccu_build_ts UTC)

    set(_ccu_build_info_file "${_output_dir}/ccu_build_info.h")
    configure_file(
        "${CMAKE_CURRENT_LIST_DIR}/ccu_build_info.h.in"
        "${_ccu_build_info_file}"
        @ONLY
    )
endfunction()
