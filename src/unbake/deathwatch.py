"""Preloaded by the pool's fork server: the server ends with the process that started it, and its workers with it."""
import ctypes
import signal

ctypes.CDLL(None).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
