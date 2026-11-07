"""The staging area's layout: names, the root marker, existing artifacts and atomic publication.

DESIGN 5.4 owns names and publication, and 5.13 owns the marker. The worker and the reconciliation
thread are the only callers: API request handlers never touch the staging share (decision C25).
"""

import stat
from collections.abc import Callable
from pathlib import Path

from pathlib_extensions import OverwritePolicy, StagedFile, build_filename, replace_os_reserved_chars

__all__ = [
    'CAPTIONS_SUFFIX', 'MARKER_NAME', 'MAX_NAME_BYTES', 'MEDIA_SUFFIX', 'captions_name', 'captions_name_for',
    'file_present', 'find_artifacts', 'marker_present', 'media_name', 'playlist_dir_name', 'publish',
    'relative_to_root',
]

MARKER_NAME = '.youtube-collector-root'
MEDIA_SUFFIX = '.mp4'
CAPTIONS_SUFFIX = '.srt'
# Decision D47 at the source revision: room is left below the 255-byte limit of common filesystems.
MAX_NAME_BYTES = 220


def marker_present(root: Path) -> bool:
    """Return whether the staging root holds its marker file, so the share is mounted.

    Any error while checking counts as absent: an unreadable root is unavailable (DESIGN 5.13).
    The marker may be any kind of entry; only its presence matters.
    """
    try:
        (root / MARKER_NAME).stat()
    except OSError:
        return False
    return True


def _id_tag(identifier: str) -> str:
    """Return the ` [id]` tag that names and lookups share, with OS-reserved characters replaced."""
    return f' [{replace_os_reserved_chars(identifier)}]'


def media_name(title: str, video_id: str) -> str:
    """Return the media file name ``Title [id].mp4``, truncating only the title to fit.

    Raises:
        ValueError: If no character of the title fits beside the ID tag and suffix.
    """
    return build_filename(title, MEDIA_SUFFIX, tag=_id_tag(video_id), max_length=MAX_NAME_BYTES)


def captions_name(title: str, video_id: str) -> str:
    """Return the captions file name ``Title [id].srt``, truncating only the title to fit.

    Raises:
        ValueError: If no character of the title fits beside the ID tag and suffix.
    """
    return build_filename(title, CAPTIONS_SUFFIX, tag=_id_tag(video_id), max_length=MAX_NAME_BYTES)


def playlist_dir_name(title: str, playlist_id: str) -> str:
    """Return a playlist folder name ``Title [playlistID]``, truncating only the title to fit.

    Raises:
        ValueError: If no character of the title fits beside the ID tag.
    """
    return build_filename(title, tag=_id_tag(playlist_id), max_length=MAX_NAME_BYTES)


def find_artifacts(directory: Path, video_id: str, suffix: str) -> list[Path]:
    """List the files in ``directory`` that are this video's artifact of one kind (decision C23).

    A file counts when its name ends with `` [<id>]<suffix>``, whatever precedes it, so a file
    keeps counting after the video's title changes. The ID is compared literally, never as a
    pattern. Only regular files count, following symlinks; directories do not.

    Args:
        directory: The destination directory: the staging root or a playlist folder.
        video_id: The video's ID.
        suffix: ``MEDIA_SUFFIX`` or ``CAPTIONS_SUFFIX``.

    Returns:
        The matching files, sorted by name; empty when ``directory`` does not exist.

    Raises:
        OSError: If the directory exists but cannot be listed, or an entry cannot be checked.
    """
    ending = _id_tag(video_id) + suffix
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    return sorted(entry for entry in entries if entry.name.endswith(ending) and file_present(entry))


def captions_name_for(title: str, video_id: str, existing_media: list[Path]) -> str:
    """Return the name for a new captions file, keeping it paired with existing media (decision C33).

    When exactly one MP4 of the video exists, the captions take its stem, so the pair stays together
    after a title change; otherwise they use the current title.

    Raises:
        ValueError: If the current title is needed and no character of it fits.
    """
    if len(existing_media) == 1:
        return existing_media[0].stem + CAPTIONS_SUFFIX
    return captions_name(title, video_id)


def publish(target: Path, write: Callable[[Path], None]) -> Path | None:
    """Write a file through a hidden stage directory beside ``target`` and publish it atomically.

    ``write`` receives the path to create inside a fresh ``.staging-<random>`` directory; a missing
    target directory is created first. Only a complete file is published, under the ``NEVER``
    policy, so nothing existing is ever replaced. If ``write`` raises, nothing is published and the
    stage directory is removed.

    Args:
        target: The final path of the file.
        write: Creates the file at the path it is given.

    Returns:
        ``target`` itself once published, or None when a file appeared there meanwhile and was kept.
        ``StagedFile`` publishes at the symlink-resolved target, but callers record and report the
        literal path, which stays inside the staging root.

    Raises:
        OSError: If staging or publication fails, including a directory at ``target``.
        Exception: Whatever ``write`` raises.
    """
    with StagedFile(target, OverwritePolicy.NEVER) as staged:
        write(staged.path)
    return None if staged.published is None else target


def file_present(path: Path) -> bool:
    """Return whether a regular file exists at ``path``, following symlinks.

    Only absence answers False; any other error propagates, so a caller never mistakes an
    unreadable share for a removed file.

    Raises:
        OSError: If ``path`` cannot be checked for a reason other than absence.
    """
    try:
        mode = path.stat().st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False
    return stat.S_ISREG(mode)


def relative_to_root(root: Path, path: Path) -> str:
    """Return ``path`` relative to the staging root in POSIX form, as responses carry it.

    Raises:
        ValueError: If ``path`` is not lexically inside ``root``.
    """
    return path.relative_to(root).as_posix()
