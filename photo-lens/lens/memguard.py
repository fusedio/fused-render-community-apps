"""Watches this process's memory during an index run, and says when to stop.

The reference library once climbed to ~11GB resident on a 16GB machine and
was killed by the OS two-thirds of the way through a run, losing every vector
computed since the last checkpoint (see embed.Embedder._release). Checkpoints
now make that loss recoverable, but the daemon still went down mid-run. This
module is the other half: catch the climb before the OS does, checkpoint, and
either recover in place or stop the run on purpose — so the daemon itself
never has to be the thing that gets killed.

The number this reads is a proxy, not a fact, and that has to be said
plainly rather than left to be discovered later:

* `psutil` RSS is what the OS accounts to the process's resident set. On
  Apple Silicon it *understates* the real footprint of an index run, because
  MPS is unified memory — torch's allocator caches GPU-side blocks that the
  OS does not attribute to RSS the same way ordinary heap pages are, and
  those blocks are exactly what an image encoder run leaves behind between
  batches (see embed.Embedder._release, which frees them one batch at a
  time but cannot make the OS report them any sooner than it does).
* `torch.mps.current_allocated_memory()` is the one number that reliably
  moves with that GPU-side allocation. Added to RSS, it is the best proxy
  this process can produce without a private API — not exact, but it is the
  number that actually moves when a run climbs toward the wall.
* `resource.getrusage(RUSAGE_SELF).ru_maxrss` is the peak RSS this process
  has ever reached, which — unlike current RSS — survives a moment of high
  water even if a later measurement missed it. Its *unit* differs by OS:
  bytes on macOS/BSD, kilobytes on Linux. That is corrected once, here,
  rather than by every caller re-deriving it.
"""
import gc
import platform

import psutil

# The daemon runs bare, on the user's own machine, indexing whatever library
# they pointed it at — there is no fleet-wide answer to "how much RAM is
# safe to use", so a config key lets each machine say its own. This is only
# the default a fresh config starts from (see config.DEFAULTS).
DEFAULT_LIMIT_GB = 8

_GB = 1024 ** 3

# ru_maxrss's unit differs by OS: bytes on Darwin/BSD, kilobytes on Linux.
# Getting this wrong reports gigabytes as kilobytes of "safety" — a limit
# that can never be reached because the number read for it is a thousand
# times too small.
_RUSAGE_IS_KB = platform.system() != "Darwin"


def current_rss_gb() -> float:
    """This process's resident set right now, in GB. See the module
    docstring for what this misses on Apple Silicon."""
    return psutil.Process().memory_info().rss / _GB


def peak_rss_gb() -> float:
    """The highest resident-set size this process has reached since it
    started, in GB — a high-water mark that survives even if nothing is
    polling `current_rss_gb()` at the moment it happens."""
    import resource
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    kb = raw if _RUSAGE_IS_KB else raw / 1024
    return kb / (1024 * 1024)


def mps_allocated_gb() -> float:
    """GPU-side blocks torch's MPS allocator currently holds, in GB — 0.0 if
    torch is not installed, MPS is not built in, or the call itself fails.
    Best-effort deliberately: a memory guard that raises is worse than one
    that reports zero for the one number it could not read."""
    try:
        import torch
        if hasattr(torch, "mps"):
            return torch.mps.current_allocated_memory() / _GB
    except Exception:
        pass
    return 0.0


def footprint_gb() -> float:
    """The best available proxy for this process's real memory use right
    now: resident RSS plus whatever the MPS allocator is holding beyond it.
    Not exact — see the module docstring — but it is the number that moved
    when the reference run climbed toward its OOM kill, which is the only
    property a guard built to prevent a repeat actually needs."""
    return current_rss_gb() + mps_allocated_gb()


def release() -> None:
    """Hand back whatever can be handed back, after a soft breach.

    The same release the embedder already does per batch (see
    embed.Embedder._release) — reachable here without an Embedder instance,
    because a breach can be noticed between batches, inside the face pass, or
    anywhere else a flush point falls. Best-effort: an older torch with no
    `torch.mps` entry point, or no torch at all, just skips straight to the
    GC pass, which costs nothing to attempt."""
    try:
        import torch
        if hasattr(torch, "mps"):
            torch.mps.empty_cache()
    except Exception:
        pass
    gc.collect()


class MemGuard:
    """One of these per index run. Call `check()` after every flush point;
    it never raises and never frees anything itself — it only reports what
    the caller should do, so the caller (which alone knows how to checkpoint
    itself) decides what "gracefully" means.

    The state machine is two breaches, not one:

    * **soft** — the footprint is over the limit for the first time since the
      last time it wasn't. The caller checkpoints and calls `release()`, and
      that is nearly free next to the embedding work already done, so paying
      it on a single momentary spike costs nothing.
    * **hard** — the footprint is *still* over the limit on the very next
      check, immediately after that cleanup ran. Freeing did not help, which
      means the memory is not coming back on its own, and the alternative to
      stopping is the OOM kill this guard exists to prevent — which would
      discard the checkpoint that just ran. The caller aborts the run.

    A limit that is zero or negative is "no limit": some machines have no
    ceiling worth enforcing, and a guard that can never be satisfied is not
    the safe default for that case — it is a run that always aborts.

    `footprint_fn` is `footprint_gb` by default; tests inject a fake one that
    returns whatever sequence of numbers the test needs, so a breach can be
    produced without allocating a gigabyte of anything.
    """

    def __init__(self, limit_gb: float, footprint_fn=footprint_gb):
        self.limit_gb = float(limit_gb)
        self._footprint_fn = footprint_fn
        # True exactly when the *previous* check was a breach — the fact that
        # makes "still above, on the very next check" checkable at all.
        self._breached = False
        # The highest footprint this guard has actually observed, across every
        # check() call — the run's own record of what it saw, independent of
        # (and a fallback for, on non-macOS or a very short run) peak_rss_gb().
        self.peak_gb = 0.0

    def check(self):
        """Returns `(status, current_gb)`, `status` one of "ok", "soft",
        "hard". Call this and nothing else — the two breach behaviours above
        belong to the caller, not to this method."""
        gb = self._footprint_fn()
        self.peak_gb = max(self.peak_gb, gb)
        if self.limit_gb <= 0 or gb <= self.limit_gb:
            self._breached = False
            return "ok", gb
        status = "hard" if self._breached else "soft"
        self._breached = True
        return status, gb
