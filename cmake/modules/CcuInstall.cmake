# CcuInstall.cmake - CCU Build 标准安装辅助
#
# 强制 CCU CMake 接入契约（设计 §6）：
#   * 使用 GNUInstallDirs 标准目录
#   * 使用命名 install component（runtime vs development）
#   * 使用 ccu:: 命名空间导出 target
#   * 支持 CMAKE_INSTALL_PREFIX 和 DESTDIR staging

include(GNUInstallDirs)

# ccu_install_target(<target>
#   [RUNTIME_COMPONENT <component>]     (default: <target>-runtime)
#   [DEVELOPMENT_COMPONENT <comp>]      (default: <target>-development)
#   [EXPORT <export-name>])
function(ccu_install_target _target)
    set(_one_value RUNTIME_COMPONENT DEVELOPMENT_COMPONENT EXPORT)
    cmake_parse_arguments(_CCU_INSTALL "" "${_one_value}" "" ${ARGN})

    if(NOT _CCU_INSTALL_RUNTIME_COMPONENT)
        set(_CCU_INSTALL_RUNTIME_COMPONENT "${_target}-runtime")
    endif()
    if(NOT _CCU_INSTALL_DEVELOPMENT_COMPONENT)
        set(_CCU_INSTALL_DEVELOPMENT_COMPONENT "${_target}-development")
    endif()

    install(TARGETS ${_target}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
            COMPONENT ${_CCU_INSTALL_RUNTIME_COMPONENT}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
            COMPONENT ${_CCU_INSTALL_RUNTIME_COMPONENT}
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
            COMPONENT ${_CCU_INSTALL_DEVELOPMENT_COMPONENT}
    )

    if(_CCU_INSTALL_EXPORT)
        install(EXPORT ${_CCU_INSTALL_EXPORT}
            DESTINATION ${CMAKE_INSTALL_LIBDIR}/cmake/${_CCU_INSTALL_EXPORT}
            NAMESPACE ccu::
            FILE ${_CCU_INSTALL_EXPORT}-targets.cmake
            COMPONENT ${_CCU_INSTALL_DEVELOPMENT_COMPONENT}
        )
    endif()
endfunction()

# ccu_install_header(<file> <destination> [COMPONENT <comp>])
function(ccu_install_header _file _dest)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_CCU_HDR "" "${_one_value}" "" ${ARGN})
    if(NOT _CCU_HDR_COMPONENT)
        set(_CCU_HDR_COMPONENT "${PROJECT_NAME}-development")
    endif()
    install(FILES ${_file}
        DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/ccu/${_dest}
        COMPONENT ${_CCU_HDR_COMPONENT}
    )
endfunction()

# ccu_install_systemd_unit(<unit-file> [COMPONENT <comp>])
function(ccu_install_systemd_unit _unit_file)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_CCU_UNIT "" "${_one_value}" "" ${ARGN})
    if(NOT _CCU_UNIT_COMPONENT)
        set(_CCU_UNIT_COMPONENT "${PROJECT_NAME}-runtime")
    endif()
    install(FILES ${_unit_file}
        DESTINATION ${CMAKE_INSTALL_LIBDIR}/systemd/system
        COMPONENT ${_CCU_UNIT_COMPONENT}
    )
endfunction()

# ccu_install_config(<config-file> [COMPONENT <comp>])
function(ccu_install_config _config_file)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_CCU_CFG "" "${_one_value}" "" ${ARGN})
    if(NOT _CCU_CFG_COMPONENT)
        set(_CCU_CFG_COMPONENT "${PROJECT_NAME}-runtime")
    endif()
    install(FILES ${_config_file}
        DESTINATION ${CMAKE_INSTALL_SYSCONFDIR}/ccu
        COMPONENT ${_CCU_CFG_COMPONENT}
    )
endfunction()

# ccu_install_default_config(<service-id> <default-yaml> [COMPONENT <comp>])
#   Precisely install a service default config template, renamed to
#   /etc/ccu/conf.d/<service-id>.yaml (设计 §9.2). Wildcard install of the
#   entire config/ directory is forbidden; config/dev/** is never installed.
function(ccu_install_default_config _service_id _default_file)
    set(_one_value COMPONENT)
    cmake_parse_arguments(_CCU_DCFG "" "${_one_value}" "" ${ARGN})
    if(NOT _CCU_DCFG_COMPONENT)
        set(_CCU_DCFG_COMPONENT "${PROJECT_NAME}-runtime")
    endif()
    install(FILES ${_default_file}
        DESTINATION ${CMAKE_INSTALL_SYSCONFDIR}/ccu/conf.d
        RENAME ${_service_id}.yaml
        COMPONENT ${_CCU_DCFG_COMPONENT}
    )
endfunction()
