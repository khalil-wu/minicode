cmake_minimum_required(VERSION 3.21)
project(cef_sidebar_probe LANGUAGES C CXX)

set(CMAKE_CONFIGURATION_TYPES Debug Release)
set(CEF_ROOT "" CACHE PATH "Extracted pinned CEF distribution")
list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT}/cmake")
find_package(CEF REQUIRED)
if(NOT OS_WINDOWS OR NOT USE_SANDBOX)
  message(FATAL_ERROR "This probe requires Windows and the CEF sandbox bootstrap")
endif()

add_subdirectory("${CEF_LIBCEF_DLL_WRAPPER_PATH}" libcef_dll_wrapper)
ADD_LOGICAL_TARGET(libcef_lib "${CEF_LIB_DEBUG}" "${CEF_LIB_RELEASE}")
SET_CEF_TARGET_OUT_DIR()

# Matches the pinned cefsimple Windows DLL/bootstrap build; the console
# bootstrap provides the smoke protocol without an addon in the Electron host.
add_library(cef_sidebar_probe SHARED sidebar_probe.cc)
SET_LIBRARY_TARGET_PROPERTIES(cef_sidebar_probe)
target_link_libraries(cef_sidebar_probe PRIVATE libcef_lib libcef_dll_wrapper
  ${CEF_STANDARD_LIBS} user32.lib)
add_dependencies(cef_sidebar_probe libcef_dll_wrapper)
COPY_SINGLE_FILE(cef_sidebar_probe "${CEF_BINARY_DIR}/bootstrapc.exe"
  "${CEF_TARGET_OUT_DIR}/cef_sidebar_probe.exe")
COPY_FILES(cef_sidebar_probe "${CEF_BINARY_FILES}" "${CEF_BINARY_DIR}"
  "${CEF_TARGET_OUT_DIR}")
COPY_FILES(cef_sidebar_probe "${CEF_RESOURCE_FILES}" "${CEF_RESOURCE_DIR}"
  "${CEF_TARGET_OUT_DIR}")
SET_LPAC_ACLS(cef_sidebar_probe)
