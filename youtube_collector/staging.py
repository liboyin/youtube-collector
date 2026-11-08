"""The staging area's layout: names, the root marker, existing artifacts and atomic publication.

DESIGN 5.4 owns names and publication, and 5.13 owns the marker. The worker is the only caller: API
request handlers never touch the staging share (decision C25).
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


def _id_prefix(identifier: str) -> str:
    """Return the ``id - `` prefix that names and lookups share, with OS-reserved characters replaced."""
    return f'{replace_os_reserved_chars(identifier)} - '


def _prefixed_name(title: str, identifier: str, suffix: str = '') -> str:
    """Return ``id - title`` plus ``suffix``, truncating only the title so the name fits in the limit.

    Raises:
        ValueError: If no character of the title fits beside the prefix and suffix.
    """
    prefix = _id_prefix(identifier)
    return prefix + build_filename(title, suffix, max_length=MAX_NAME_BYTES - len(prefix.encode()))


def media_name(title: str, video_id: str) -> str:
    """Return the media file name ``id - Title.mp4``, truncating only the title to fit.

    Raises:
        ValueError: If no character of the title fits beside the ID prefix and suffix.
    """
    return _prefixed_name(title, video_id, MEDIA_SUFFIX)


def captions_name(title: str, video_id: str) -> str:
    """Return the captions file name ``id - Title.srt``, truncating only the title to fit.

    Raises:
        ValueError: If no character of the title fits beside the ID prefix and suffix.
    """
    return _prefixed_name(title, video_id, CAPTIONS_SUFFIX)


def playlist_dir_name(title: str, playlist_id: str) -> str:
    """Return a playlist folder name ``playlistID - Title``, truncating only the title to fit.

    A folder name ends with free text, unlike a file name, so trailing dots and spaces are removed:
    SMB shares do not accept them at the end of a name.

    Raises:
        ValueError: If no character of the title fits beside the ID prefix, or none is left once
            trailing dots and spaces are removed.
    """
    prefix = _id_prefix(playlist_id)
    kept = _prefixed_name(title, playlist_id).removeprefix(prefix).rstrip('. ')
    if not kept:
        raise ValueError(f'Playlist title {title!r} has nothing left to keep in a folder name')
    return prefix + kept


def find_artifacts(directory: Path, video_id: str, suffix: str) -> list[Path]:
    """List the files in ``directory`` that are this video's artifact of one kind (decision C23).

    A file counts when its name starts with ``<id> - `` and ends with ``suffix``, whatever lies
    between, so a file keeps counting after the video's title changes. The ID is compared literally,
    never as a pattern. Only regular files count, following symlinks; directories do not.

    Args:
        directory: The destination directory: the staging root or a playlist folder.
        video_id: The video's ID.
        suffix: ``MEDIA_SUFFIX`` or ``CAPTIONS_SUFFIX``.

    Returns:
        The matching files, sorted by name; empty when ``directory`` does not exist.

    Raises:
        OSError: If the directory exists but cannot be listed, or an entry cannot be checked.
    """
    prefix = _id_prefix(video_id)
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    return sorted(
        entry for entry in entries
        if entry.name.startswith(prefix) and entry.name.endswith(suffix) and file_present(entry)
    )


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
