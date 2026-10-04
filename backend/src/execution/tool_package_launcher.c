/* Trusted fixed CPython startup, not a package-selected executable. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <errno.h>
#include <stdint.h>
#include <string.h>
#include <sys/random.h>

static int checked(PyStatus status) {
    if (PyStatus_Exception(status) && status.err_msg)
        fprintf(stderr, "isolated Python startup failed: %s\n", status.err_msg);
    return !PyStatus_Exception(status);
}

int main(int argc, char **argv) {
    if (argc != 1 && !(argc == 2 && strcmp(argv[1], "preflight") == 0))
        return 2;
    uint32_t seed;
    ssize_t received = -1;
    /* No device, environment seed, sleep, short-read or unavailable fallback. */
    for (int attempt = 0; attempt < 3; ++attempt) {
        received = getrandom(&seed, sizeof(seed), GRND_NONBLOCK);
        if (received >= 0 || errno != EINTR)
            break;
    }
    if (received != sizeof(seed))
        return 3;
    PyPreConfig preconfig;
    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    if (!checked(Py_PreInitialize(&preconfig)))
        return 4;
    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.use_hash_seed = 1;
    config.hash_seed = seed;
    config.parse_argv = 0;
    config.site_import = 0;
    config.write_bytecode = 0;
    config.module_search_paths_set = 1;
    int valid = checked(PyConfig_SetString(&config, &config.program_name, L"/runtime/bin/isolated-python"))
        && checked(PyConfig_SetString(&config, &config.executable, L"/runtime/bin/isolated-python"))
        && checked(PyConfig_SetString(&config, &config.home, L"/runtime"))
        && checked(PyConfig_SetString(&config, &config.filesystem_encoding, L"utf-8"))
        && checked(PyConfig_SetString(&config, &config.filesystem_errors, L"surrogateescape"))
        && checked(PyConfig_SetString(&config, &config.stdio_encoding, L"utf-8"))
        && checked(PyConfig_SetString(&config, &config.stdio_errors, L"strict"))
        && checked(PyConfig_SetString(&config, &config.run_filename, L"/bootstrap.py"))
        && checked(PyWideStringList_Append(&config.module_search_paths, L"/runtime/lib/python3.12"))
        && checked(PyWideStringList_Append(&config.argv, L"/bootstrap.py"));
    if (valid && argc == 2)
        valid = checked(PyWideStringList_Append(&config.argv, L"preflight"));
    if (valid)
        valid = checked(Py_InitializeFromConfig(&config));
    PyConfig_Clear(&config);
    if (!valid)
        return 4;
    return Py_RunMain();
}
