"""Environment-aware paths and binary discovery.

Makes the server run unchanged on:
  * a normal Linux/macOS machine  — everything stays inside the repo folder;
  * Termux on Android             — writable data lives in the home directory
    (the repo dir on Android storage is often read-only / slow), and binaries
    are looked up in Termux's prefix (``$PREFIX/files/usr/bin`` when run from
    the Files app, otherwise the standard ``$PREFIX/bin``).

Everything can still be overridden explicitly via environment variables or a
`.env` file — see `.env.example`.
"""

import os
import shutil
from pathlib import Path


def is_termux() -> bool:
    """True when running inside a Termux environment on Android.

    Checked two ways: the env vars Termux normally sets, AND a direct
    filesystem check for Termux's install path. The env-var check alone is
    fragile — it depends on which env vars got inherited by however the
    process was *this time* started (interactive shell vs Termux:Boot vs
    termux-services vs `nohup` vs a different terminal app), so two
    launches of the exact same install can disagree. The filesystem check
    doesn't depend on the launch method at all, so it's kept as a
    belt-and-suspenders backstop rather than the primary signal (see
    ``default_data_dir`` below, which no longer branches on this at all
    for the *data* directory — only for binary lookup).
    """
    return (
        os.environ.get("TERMUX_VERSION") is not None
        or "/com.termux" in os.environ.get("PREFIX", "")
        or "/com.termux" in os.environ.get("ANDROID_ROOT", "")
        or Path("/data/data/com.termux").is_dir()
    )


def _writable(dir_path: Path) -> bool:
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        probe = dir_path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def default_data_dir() -> Path:
    """Base directory for writable data (DB, VTT cache, temp audio, logs).

    IMPORTANT: this used to branch on ``is_termux()`` and fall back to
    ``Path.cwd()`` on non-Termux-detected launches. That made the resolved
    path depend on *how* the server happened to be started, not just
    *where* it's installed — restart it a different way (a boot script, a
    detached shell, a different terminal app) and it could silently open
    a different, empty database at whatever directory happened to be the
    current working directory that time. Symptom: chapters transcribed
    yesterday would appear "not transcribed" and get redone.

    Fix: always prefer the same fixed, conventional data directory under
    the user's home — this is writable and correct on Termux, regular
    Linux, and macOS alike, so there's no environment branch to get wrong.
    Only if home genuinely isn't writable (unusual — some minimal
    containers) do we fall back to a directory next to *this file*
    (deterministic — tied to the install location, not the shell's CWD).
    """
    home_dir = Path.home() / ".local" / "share" / "audiobook-transcriber"
    if _writable(home_dir):
        return home_dir
    # Fallback: next to this script's install location, never the CWD.
    return Path(__file__).resolve().parent / "data"


def resolve_dir(env_var: str, default_name: str) -> str:
    """Resolves a writable directory.

    Order: explicit env var → Termux data dir/<name> → repo-local ./<name>.
    """
    env = os.environ.get(env_var)
    if env:
        return str(Path(env).expanduser())
    return str(default_data_dir() / default_name)


def resolve_file(env_var: str, default_name: str) -> str:
    """Resolves a writable file path (same rules as :func:`resolve_dir`)."""
    env = os.environ.get(env_var)
    if env:
        return str(Path(env).expanduser())
    return str(default_data_dir() / default_name)


def _termux_bin_dir() -> Path | None:
    """Termux binary dir, including the private-files-app prefix."""
    prefix = os.environ.get("PREFIX")
    candidates: list[Path] = []
    if prefix:
        candidates.append(Path(prefix) / "bin")
        # When launched from the Termux:Files share the prefix points at the
        # files-app dir; real binaries live one level deeper.
        candidates.append(Path(prefix) / "files" / "usr" / "bin")
    if is_termux():
        candidates.append(Path.home() / "files" / "usr" / "bin")
    for c in candidates:
        if c.is_dir():
            return c
    return None


def find_binary(name: str, env_var: str) -> str:
    """Finds an external binary.

    Order: explicit env var (e.g. FFMPEG_PATH) → PATH → Termux prefix dirs.
    Raises FileNotFoundError with an actionable hint when missing.
    """
    explicit = os.environ.get(env_var)
    if explicit:
        return str(Path(explicit).expanduser())

    found = shutil.which(name)
    if found:
        return found

    if is_termux():
        bin_dir = _termux_bin_dir()
        if bin_dir is not None:
            candidate = bin_dir / name
            if candidate.exists():
                return str(candidate)
        raise FileNotFoundError(
            f"'{name}' not found. Install it with:  pkg install {name}"
        )

    raise FileNotFoundError(
        f"'{name}' not found on PATH. Install ffmpeg (https://ffmpeg.org) "
        f"or set {env_var} to its full path."
    )


def ffmpeg_path() -> str:
    return find_binary("ffmpeg", "FFMPEG_PATH")


def ffprobe_path() -> str:
    return find_binary("ffprobe", "FFPROBE_PATH")
