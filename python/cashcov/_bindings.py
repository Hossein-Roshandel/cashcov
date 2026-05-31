"""Low-level ctypes bindings for libcashcov.so.

This module locates the shared library and declares the C-level function
signatures.  Application code should use :mod:`cashcov.client` instead.
"""

import ctypes
import os
import sys
from pathlib import Path

# libc for malloc — used by the generator callback to produce a C-owned string.
_libc = ctypes.CDLL(None)
_libc.malloc.restype = ctypes.c_void_p
_libc.malloc.argtypes = [ctypes.c_size_t]

# ---------------------------------------------------------------------------
# Library discovery
# ---------------------------------------------------------------------------
# Precedence:
#   1. CASHCOV_LIB_PATH environment variable (absolute path to the .so/.dylib)
#   2. The directory that contains this file (bundled distribution)
#   3. Standard system library search path


def _find_library() -> ctypes.CDLL:
    env_path = os.environ.get("CASHCOV_LIB_PATH")
    if env_path:
        return ctypes.CDLL(env_path)

    lib_name = {
        "linux": "libcashcov.so",
        "darwin": "libcashcov.dylib",
        "win32": "cashcov.dll",
    }.get(sys.platform, "libcashcov.so")

    # Next to this file (installed wheel layout)
    local = Path(__file__).parent / lib_name
    if local.exists():
        return ctypes.CDLL(str(local))

    # Fall back to system search (e.g. /usr/local/lib)
    return ctypes.CDLL(lib_name)


_lib = _find_library()

# ---------------------------------------------------------------------------
# Generator callback type
# The Python callable passed to get_or_refresh is wrapped in this CFUNCTYPE.
# The function receives the cache key and must return a malloc'd C string
# (as a plain integer / c_void_p) containing the JSON-encoded value, or 0/None
# to signal an error.  The C shim takes ownership and frees the returned pointer.
# ---------------------------------------------------------------------------

# typedef char* (*cashcov_generator_fn)(const char* key);
# Return type is c_void_p so ctypes does NOT do the bytes-copy conversion that
# c_char_p would apply, allowing the callback to return a raw malloc'd address.
GENERATOR_FN = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_char_p)

# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------

# int64_t CashCov_NewHandler(const char* redisAddr, const char* configJSON);
_lib.CashCov_NewHandler.restype = ctypes.c_int64
_lib.CashCov_NewHandler.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

# char* CashCov_GetOrRefresh(int64_t handle, const char* key, cashcov_generator_fn gen,
#                            int missFillPolicy, int hitRefreshPolicy, int errorPolicy);
# Pass -1 for any policy to use the handler-level default.
# restype is c_void_p so we retain the raw pointer for CashCov_Free; c_char_p
# would silently copy the bytes into a Python object and lose the address.
_lib.CashCov_GetOrRefresh.restype = ctypes.c_void_p
_lib.CashCov_GetOrRefresh.argtypes = [
    ctypes.c_int64,
    ctypes.c_char_p,
    GENERATOR_FN,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
]

# int CashCov_Set(int64_t handle, const char* key, const char* value, int ttlSecs);
_lib.CashCov_Set.restype = ctypes.c_int
_lib.CashCov_Set.argtypes = [ctypes.c_int64, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]

# void CashCov_SetGenerator(int64_t handle, cashcov_generator_fn gen);
# Registers a persistent background generator for the handle.  Background goroutines
# (hit-refresh, stale-rewrite) use this generator, not the per-call one.
# Pass NULL to clear.
_lib.CashCov_SetGenerator.restype = None
_lib.CashCov_SetGenerator.argtypes = [ctypes.c_int64, GENERATOR_FN]

# void CashCov_DestroyHandler(int64_t handle);
_lib.CashCov_DestroyHandler.restype = None
_lib.CashCov_DestroyHandler.argtypes = [ctypes.c_int64]

# void CashCov_Free(char* ptr);
_lib.CashCov_Free.restype = None
_lib.CashCov_Free.argtypes = [ctypes.c_void_p]
