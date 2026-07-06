"""millet-record — lightweight capture-only subset of millet (formerly meetscribe-record).

Public modules:
    millet_record.capture    — RecordingSession, dual-channel capture
                             (Linux: ffmpeg+PulseAudio; macOS 14.4+
                             arm64: meet-record-mac sidecar)
    millet_record.audio      — stereo channel reading + ffmpeg compression
    millet_record.utils      — formatting helpers
    millet_record.languages  — language constants
    millet_record.cli        — `millet` console-script entry point
                             (with deprecation-aliased `meet` for two
                              minor versions)

Named after the Ottoman millet system.  Part of the vezir ecosystem.

Version is the single source of truth here; pyproject.toml's
[project] section pulls it dynamically via setuptools.dynamic.
"""

__version__ = "0.5.0"

# ── Backward-compat: meet_record alias ──────────────────────────────────────
# Existing code (e.g. older meetscribe-offline 0.8.3 compatibility shims,
# third-party scripts) imports ``from meet_record.X import …``.  We register
# this package as both ``millet_record`` (canonical) and ``meet_record``
# (legacy) in sys.modules so both import paths resolve to the same package.
# Submodules are also aliased lazily via a MetaPathFinder so we don't pay
# the import cost up-front (capture pulls ffmpeg detection on Linux).
#
# Removed in millet-record 0.6.0 (matches the `meet` console-script
# deprecation timeline).
import importlib as _importlib
import importlib.abc as _abc
import importlib.machinery as _machinery
import sys as _sys


class _MeetRecordAliasFinder(_abc.MetaPathFinder):
    """Resolve ``meet_record`` and ``meet_record.X`` to ``millet_record[.X]``."""

    def find_spec(self, fullname, path, target=None):
        if fullname == "meet_record":
            return _machinery.ModuleSpec(
                fullname,
                loader=_MeetRecordAliasLoader(),
                is_package=True,
            )
        if fullname.startswith("meet_record."):
            new_name = "millet_record." + fullname[len("meet_record."):]
            try:
                mod = _importlib.import_module(new_name)
            except ImportError:
                return None
            _sys.modules[fullname] = mod
            return _machinery.ModuleSpec(
                fullname,
                loader=_MeetRecordAliasLoader(),
                is_package=hasattr(mod, "__path__"),
            )
        return None


class _MeetRecordAliasLoader(_abc.Loader):
    def create_module(self, spec):
        if spec.name == "meet_record":
            return _sys.modules[__name__]
        new_name = "millet_record." + spec.name[len("meet_record."):]
        return _sys.modules.get(new_name) or _importlib.import_module(new_name)

    def exec_module(self, module):
        return None


_sys.modules.setdefault("meet_record", _sys.modules[__name__])
if not any(isinstance(f, _MeetRecordAliasFinder) for f in _sys.meta_path):
    _sys.meta_path.append(_MeetRecordAliasFinder())
