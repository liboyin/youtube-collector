"""Tests for the staging layout (DESIGN 5.4 and 5.13)."""

import os

import pytest

import youtube_collector.staging as testee

VIDEO_ID = 'dQw4w9WgXcQ'


def test_marker_present_reports_the_marker(tmp_path):
    """The marker's presence, of any entry type, decides availability."""
    assert not testee.marker_present(tmp_path)
    (tmp_path / testee.MARKER_NAME).touch()
    assert testee.marker_present(tmp_path)


def test_marker_present_treats_a_marker_directory_as_present(tmp_path):
    """Only presence matters, so a marker created as a directory counts."""
    (tmp_path / testee.MARKER_NAME).mkdir()
    assert testee.marker_present(tmp_path)


def test_marker_present_treats_a_missing_root_as_absent(tmp_path):
    """A root that does not exist, as on an unmounted share, is unavailable."""
    assert not testee.marker_present(tmp_path / 'unmounted')


def test_marker_present_treats_errors_as_absent(tmp_path):
    """Any error while checking the marker counts as absent rather than raising."""
    root = tmp_path / 'file'
    root.touch()  # a root that is a file makes the check fail with NotADirectoryError
    assert not testee.marker_present(root)


def test_media_name_puts_the_id_before_the_title():
    """Media is named id - Title.mp4."""
    assert testee.media_name('A title', VIDEO_ID) == f'{VIDEO_ID} - A title.mp4'


def test_media_name_replaces_reserved_characters_in_title_and_id():
    """OS-reserved characters are replaced in both the title and the ID."""
    assert testee.media_name('a/b: c?', 'x/y') == 'x_y - a_b_ c_.mp4'


def test_media_name_truncates_only_the_title_to_220_bytes():
    """A long multi-byte title is cut at a character boundary so the whole name fits in 220 bytes."""
    name = testee.media_name('字' * 100, VIDEO_ID)
    prefix = f'{VIDEO_ID} - '
    assert len(name.encode()) <= testee.MAX_NAME_BYTES
    assert name.startswith(prefix)
    assert name.endswith('.mp4')
    assert name.removeprefix(prefix).removesuffix('.mp4') == '字' * ((220 - len(prefix) - len('.mp4')) // 3)


def test_media_name_uses_the_whole_limit_for_a_long_title():
    """A single-byte title fills the limit exactly: the prefix counts once."""
    assert len(testee.media_name('a' * 300, VIDEO_ID).encode()) == testee.MAX_NAME_BYTES


def test_media_name_rejects_a_title_with_nothing_to_keep():
    """An empty title leaves nothing to keep and fails."""
    with pytest.raises(ValueError):
        testee.media_name('', VIDEO_ID)


def test_media_name_rejects_an_id_that_leaves_no_room():
    """An ID too long to leave room for any title character fails."""
    with pytest.raises(ValueError):
        testee.media_name('title', 'x' * 220)


def test_captions_name_puts_the_id_before_the_title():
    """Captions are named id - Title.srt, truncated like media."""
    assert testee.captions_name('A title', VIDEO_ID) == f'{VIDEO_ID} - A title.srt'
    assert len(testee.captions_name('a' * 300, VIDEO_ID).encode()) == testee.MAX_NAME_BYTES


def test_playlist_dir_name_puts_the_playlist_id_before_the_title():
    """Playlist folders are named playlistID - Title, with no suffix inferred from dots."""
    assert testee.playlist_dir_name('Mix v1.2', 'PLabc') == 'PLabc - Mix v1.2'
    assert len(testee.playlist_dir_name('a' * 300, 'PLabc').encode()) == testee.MAX_NAME_BYTES


@pytest.mark.parametrize('title', ['Best of...', 'Spaces   ', 'Both. . '])
def test_playlist_dir_name_drops_trailing_dots_and_spaces(title):
    """A folder name cannot end in a dot or space on an SMB share, so they are removed."""
    assert testee.playlist_dir_name(title, 'PLabc') == 'PLabc - ' + title.rstrip('. ')


@pytest.mark.parametrize('title', ['...', '   ', '. .', '.', '..'])
def test_playlist_dir_name_rejects_a_title_of_only_dots_and_spaces(title):
    """A title that disappears once stripped has nothing left to keep and fails (DESIGN 5.4)."""
    with pytest.raises(ValueError):
        testee.playlist_dir_name(title, 'PLabc')


def test_media_names_keep_a_title_that_ends_in_a_dot():
    """Only folder names are stripped: a file name always ends in its suffix."""
    assert testee.media_name('Best of...', VIDEO_ID) == f'{VIDEO_ID} - Best of....mp4'


def test_find_artifacts_matches_any_title_with_the_id_and_suffix(tmp_path):
    """Files are matched by ID and suffix, whatever the title part says (C23, C45)."""
    old = tmp_path / f'{VIDEO_ID} - Old title.mp4'
    new = tmp_path / f'{VIDEO_ID} - New title.mp4'
    old.touch()
    new.touch()
    (tmp_path / f'{VIDEO_ID} - Other.srt').touch()
    (tmp_path / f'{VIDEO_ID} - Other.mp4.part').touch()
    (tmp_path / f'x{VIDEO_ID} - Other.mp4').touch()
    (tmp_path / f'{VIDEO_ID}x - Other.mp4').touch()
    (tmp_path / f'{VIDEO_ID}- Other.mp4').touch()
    (tmp_path / f'{VIDEO_ID} - Other.MP4').touch()
    assert testee.find_artifacts(tmp_path, VIDEO_ID, testee.MEDIA_SUFFIX) == [new, old]


def test_find_artifacts_does_not_recognize_the_old_name_format(tmp_path):
    """Files named Title [id].ext are not artifacts: nothing keeps backward compatibility (C44)."""
    (tmp_path / f'Title [{VIDEO_ID}].mp4').touch()
    assert testee.find_artifacts(tmp_path, VIDEO_ID, testee.MEDIA_SUFFIX) == []


def test_find_artifacts_matches_an_empty_title_part(tmp_path):
    """Whatever lies between the prefix and the suffix counts, including nothing."""
    path = tmp_path / f'{VIDEO_ID} - .mp4'
    path.touch()
    assert testee.find_artifacts(tmp_path, VIDEO_ID, testee.MEDIA_SUFFIX) == [path]


def test_find_artifacts_treats_the_id_literally(tmp_path):
    """Glob characters in an ID match only themselves."""
    (tmp_path / 'abc - Title.mp4').touch()
    assert testee.find_artifacts(tmp_path, '*', testee.MEDIA_SUFFIX) == []
    assert testee.find_artifacts(tmp_path, '[a-c]bc', testee.MEDIA_SUFFIX) == []
    (tmp_path / 'a?c - Title.mp4').touch()
    assert testee.find_artifacts(tmp_path, 'a?c', testee.MEDIA_SUFFIX) == []  # reserved ? became _
    assert testee.find_artifacts(tmp_path, 'abc', testee.MEDIA_SUFFIX) == [tmp_path / 'abc - Title.mp4']


def test_find_artifacts_matches_an_id_that_starts_with_a_dash(tmp_path):
    """YouTube IDs can begin with a dash, which is an ordinary character here."""
    path = tmp_path / '-abcdefghij - Title.mp4'
    path.touch()
    assert testee.find_artifacts(tmp_path, '-abcdefghij', testee.MEDIA_SUFFIX) == [path]


def test_find_artifacts_uses_the_same_prefix_as_names(tmp_path):
    """An ID with reserved characters is found under the name media_name gives it."""
    path = tmp_path / testee.media_name('Title', 'a?c')
    path.touch()
    assert testee.find_artifacts(tmp_path, 'a?c', testee.MEDIA_SUFFIX) == [path]


def test_find_artifacts_counts_files_and_symlinks_to_files_only(tmp_path):
    """Directories and dangling symlinks do not count; a symlink to a file does."""
    (tmp_path / f'{VIDEO_ID} - Dir.mp4').mkdir()
    (tmp_path / f'{VIDEO_ID} - Dangling.mp4').symlink_to(tmp_path / 'missing')
    real = tmp_path / 'real'
    real.touch()
    link = tmp_path / f'{VIDEO_ID} - Link.mp4'
    link.symlink_to(real)
    assert testee.find_artifacts(tmp_path, VIDEO_ID, testee.MEDIA_SUFFIX) == [link]


def test_find_artifacts_returns_nothing_for_a_missing_directory(tmp_path):
    """A playlist folder the user moved away holds no artifacts."""
    assert testee.find_artifacts(tmp_path / 'moved', VIDEO_ID, testee.MEDIA_SUFFIX) == []


def test_find_artifacts_propagates_listing_errors(tmp_path):
    """A destination that cannot be listed raises instead of reporting no artifacts."""
    not_a_dir = tmp_path / 'file'
    not_a_dir.touch()
    with pytest.raises(NotADirectoryError):
        testee.find_artifacts(not_a_dir, VIDEO_ID, testee.MEDIA_SUFFIX)


def test_captions_name_for_pairs_with_a_single_existing_mp4(tmp_path):
    """New captions take the stem of the only existing MP4 (C33)."""
    media = tmp_path / f'{VIDEO_ID} - Old title.mp4'
    assert testee.captions_name_for('New title', VIDEO_ID, [media]) == f'{VIDEO_ID} - Old title.srt'


@pytest.mark.parametrize('count', [0, 2])
def test_captions_name_for_uses_the_current_title_otherwise(tmp_path, count):
    """Without exactly one MP4, captions use the current title."""
    media = [tmp_path / f'{VIDEO_ID} - Title {n}.mp4' for n in range(count)]
    assert testee.captions_name_for('New title', VIDEO_ID, media) == f'{VIDEO_ID} - New title.srt'


def test_publish_writes_the_file_and_creates_its_directory(tmp_path):
    """A completed write is published at the target, inside a newly created directory."""
    target = tmp_path / 'PL - Playlist' / 'a.mp4'

    def write(path):
        assert path.parent.parent == target.parent
        assert path.parent.name.startswith('.staging-')
        path.write_bytes(b'media')

    assert testee.publish(target, write) == target
    assert target.read_bytes() == b'media'
    assert os.listdir(target.parent) == ['a.mp4']


def test_publish_keeps_a_file_that_appeared_meanwhile(tmp_path):
    """Under NEVER, a competing file at the target is kept and the stage is discarded."""
    target = tmp_path / 'a.srt'

    def write(path):
        target.write_bytes(b'competing')
        path.write_bytes(b'ours')

    assert testee.publish(target, write) is None
    assert target.read_bytes() == b'competing'
    assert os.listdir(tmp_path) == ['a.srt']


def test_publish_never_replaces_an_existing_file(tmp_path):
    """An existing target is never overwritten, even when the writer is called."""
    target = tmp_path / 'a.mp4'
    target.write_bytes(b'existing')
    assert testee.publish(target, lambda path: path.write_bytes(b'new')) is None
    assert target.read_bytes() == b'existing'
    assert os.listdir(tmp_path) == ['a.mp4']


def test_publish_leaves_nothing_when_the_writer_fails(tmp_path):
    """A failing writer publishes nothing and removes its stage directory."""
    target = tmp_path / 'a.mp4'

    def write(path):
        path.write_bytes(b'partial')
        raise RuntimeError('download failed')

    with pytest.raises(RuntimeError, match='download failed'):
        testee.publish(target, write)
    assert os.listdir(tmp_path) == []


def test_publish_fails_when_nothing_was_written(tmp_path):
    """A writer that creates no file is an error, not a silent success."""
    with pytest.raises(FileNotFoundError):
        testee.publish(tmp_path / 'a.mp4', lambda path: None)
    assert os.listdir(tmp_path) == []


def test_publish_returns_the_literal_target_under_a_symlinked_directory(tmp_path):
    """The reported path stays the literal one when the destination directory is a symlink."""
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    target = link / 'a.mp4'
    assert testee.publish(target, lambda path: path.write_bytes(b'x')) == target
    assert (real / 'a.mp4').read_bytes() == b'x'


def test_file_present_reports_regular_files_only(tmp_path):
    """Files and symlinks to files are present; directories and missing paths are not."""
    file = tmp_path / 'f'
    file.touch()
    link = tmp_path / 'l'
    link.symlink_to(file)
    assert testee.file_present(file)
    assert testee.file_present(link)
    assert not testee.file_present(tmp_path)
    assert not testee.file_present(tmp_path / 'missing')
    assert not testee.file_present(file / 'below-a-file')


def test_file_present_propagates_other_errors(tmp_path):
    """An error other than absence raises, so it is never mistaken for a removed file."""
    loop = tmp_path / 'loop'
    loop.symlink_to(loop)
    with pytest.raises(OSError):
        testee.file_present(loop)


def test_relative_to_root_gives_posix_paths(tmp_path):
    """Paths inside the root are reported relative to it."""
    assert testee.relative_to_root(tmp_path, tmp_path / 'x - P' / 'a.mp4') == 'x - P/a.mp4'


def test_relative_to_root_rejects_paths_outside_the_root(tmp_path):
    """A path outside the staging root is an error."""
    with pytest.raises(ValueError):
        testee.relative_to_root(tmp_path / 'root', tmp_path / 'a.mp4')
