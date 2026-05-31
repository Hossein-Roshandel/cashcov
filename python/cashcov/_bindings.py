"""Low-level ctypes bindings for libcashcov.so.

This module locates the shared library and declares the C-level function
signatures.  Application code should use :mod:`cashcov.client` instead.
"""

import ctypes
import os
import sys
from pathlib import Path

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
# The function receives the cache key and must return a bytes object containing
# the JSON-encoded value, or None to signal an error.  The returned bytes are
# copied into a malloc'd C string by the wrapper; Python retains ownership of
# the bytes object itself.
# ---------------------------------------------------------------------------

# typedef char* (*cashcov_generator_fn)(const char* key);
GENERATOR_FN = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_char_p)

# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------

# int64_t CashCov_NewHandler(const char* redisAddr, const char* configJSON);
_lib.CashCov_NewHandler.restype = ctypes.c_int64
_lib.CashCov_NewHandler.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

# char* CashCov_GetOrRefresh(int64_t handle, const char* key, cashcov_generator_fn gen,
#                            int missFillPolicy, int hitRefreshPolicy, int errorPolicy);
# Pass -1 for any policy to use the handler-level default.
_lib.CashCov_GetOrRefresh.restype = ctypes.c_char_p
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

# void CashCov_DestroyHandler(int64_t handle);
_lib.CashCov_DestroyHandler.restype = None
_lib.CashCov_DestroyHandler.argtypes = [ctypes.c_int64]

# void CashCov_Free(char* ptr);
_lib.CashCov_Free.restype = None
_lib.CashCov_Free.argtypes = [ctypes.c_char_p]
