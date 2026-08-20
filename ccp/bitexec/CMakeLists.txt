cmake_minimum_required(VERSION 3.16)
project(ccp_bitexec CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

# -O3 matters here in a way it does not elsewhere in the project: this is the
# only layer where the instruction count per byte is the product's throughput.
# No -march=native: the AVX2 path is selected at runtime by CPU detection, so the
# binary stays portable across the machines a build might be shipped to.
add_library(ccp_bitexec STATIC src/bitexec.cpp)
target_include_directories(ccp_bitexec PUBLIC include)
target_compile_options(ccp_bitexec PRIVATE -O3 -Wall -Wextra -Wpedantic)

add_executable(test_bitexec tests/test_bitexec.cpp)
target_link_libraries(test_bitexec PRIVATE ccp_bitexec)
target_compile_options(test_bitexec PRIVATE -O2 -Wall -Wextra)

enable_testing()
add_test(NAME bitexec COMMAND test_bitexec)
