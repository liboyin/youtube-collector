"""Tests for URL parsing (DESIGN 5.2)."""

import pytest

import youtube_collector.urls as testee

VIDEO_ID = 'dQw4w9WgXcQ'
HOSTS = ['youtube.com', 'www.youtube.com', 'm.youtube.com']


def test_video_target_url_is_canonical_watch_url():
    """A video target's URL is the canonical watch URL with no playlist."""
    assert testee.VideoTarget(VIDEO_ID).url == f'https://www.youtube.com/watch?v={VIDEO_ID}'


def test_playlist_target_url_is_canonical_playlist_url():
    """A playlist target's URL is the canonical playlist URL."""
    assert testee.PlaylistTarget('PLabc').url == 'https://www.youtube.com/playlist?list=PLabc'


@pytest.mark.parametrize('scheme', ['http', 'https', 'HTTPS'])
@pytest.mark.parametrize('host', HOSTS)
@pytest.mark.parametrize('path', [
    f'/watch?v={VIDEO_ID}', f'/watch/?v={VIDEO_ID}', f'/live/{VIDEO_ID}', f'/live/{VIDEO_ID}/',
    f'/shorts/{VIDEO_ID}', f'/shorts/{VIDEO_ID}/',
])
def test_parse_target_accepts_every_video_form_on_every_host(scheme, host, path):
    """Watch, live and shorts URLs on every YouTube host and scheme name the video."""
    assert testee.parse_target(f'{scheme}://{host}{path}') == testee.VideoTarget(VIDEO_ID)


@pytest.mark.parametrize('url', [
    f'https://youtu.be/{VIDEO_ID}', f'http://youtu.be/{VIDEO_ID}/', f'https://YOUTU.BE/{VIDEO_ID}?si=abc&t=10',
])
def test_parse_target_accepts_short_links(url):
    """youtu.be links name the video, ignoring query parameters."""
    assert testee.parse_target(url) == testee.VideoTarget(VIDEO_ID)


@pytest.mark.parametrize('url', [
    f'https://www.youtube.com/watch?v={VIDEO_ID}&list=PLabc',
    f'https://www.youtube.com/watch?list=PLabc&v={VIDEO_ID}&index=3',
    f'https://youtu.be/{VIDEO_ID}?list=PLabc',
    f'https://www.youtube.com/watch?v={VIDEO_ID}&list=RDxyz',
])
def test_parse_target_treats_watch_url_with_list_as_single_video(url):
    """A video URL carrying list= is the single video, never the playlist (C16)."""
    assert testee.parse_target(url) == testee.VideoTarget(VIDEO_ID)


def test_parse_target_ignores_other_parameters_and_fragments():
    """Unrelated parameters and fragments do not change the target."""
    url = f'https://www.youtube.com/watch?feature=share&v={VIDEO_ID}&t=42s#comments'
    assert testee.parse_target(url) == testee.VideoTarget(VIDEO_ID)


@pytest.mark.parametrize('host', HOSTS)
@pytest.mark.parametrize('playlist_id', ['PLabc-_123', 'OLAK5uy_x', 'UUabc', 'WL', 'rdlower'])
def test_parse_target_accepts_playlists(host, playlist_id):
    """Playlist URLs on every host name the playlist, including IDs that only lowercase-start with rd."""
    assert testee.parse_target(f'https://{host}/playlist?list={playlist_id}') == testee.PlaylistTarget(playlist_id)


@pytest.mark.parametrize('playlist_id', ['RD', f'RD{VIDEO_ID}', 'RDMMabc', 'RDCLAK5uy_x'])
def test_parse_target_rejects_mix_playlists(playlist_id):
    """Playlist IDs starting RD are Mixes and rejected (C21)."""
    with pytest.raises(testee.UnsupportedUrlError, match='Mix'):
        testee.parse_target(f'https://www.youtube.com/playlist?list={playlist_id}')


@pytest.mark.parametrize('path', [
    '/channel/UCabc', '/channel/UCabc/videos', '/c/name', '/user/name', '/@handle', '/@handle/', '/@handle/playlists',
])
def test_parse_target_rejects_channels_handles_and_tabs(path):
    """Channel, handle and channel-tab URLs are rejected with a channel-specific reason."""
    with pytest.raises(testee.UnsupportedUrlError, match='Channel'):
        testee.parse_target(f'https://www.youtube.com{path}')


@pytest.mark.parametrize('url', [
    f'https://www.youtube.com/watch?v={VIDEO_ID[:-1]}',
    f'https://www.youtube.com/watch?v={VIDEO_ID}x',
    'https://www.youtube.com/watch?v=dQw4w9WgXc!',
    'https://www.youtube.com/watch?v=',
    f'https://youtu.be/{VIDEO_ID[:-1]}',
    'https://youtu.be/',
    'https://www.youtube.com/shorts/short',
    'https://www.youtube.com/playlist?list=PL%20abc',
    'https://www.youtube.com/playlist?list=',
])
def test_parse_target_rejects_malformed_ids(url):
    """Video and playlist IDs of the wrong shape are rejected."""
    with pytest.raises(testee.UnsupportedUrlError, match='valid'):
        testee.parse_target(url)


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch',
    'https://www.youtube.com/watch?list=PLabc',
    f'https://www.youtube.com/watch?v={VIDEO_ID}&v={VIDEO_ID}',
    'https://www.youtube.com/playlist',
    'https://www.youtube.com/playlist?list=PLa&list=PLb',
])
def test_parse_target_rejects_missing_or_repeated_parameters(url):
    """A watch or playlist URL needs exactly one v= or list= value."""
    with pytest.raises(testee.UnsupportedUrlError, match='exactly one'):
        testee.parse_target(url)


@pytest.mark.parametrize('url', [
    f'https://youtu.be/{VIDEO_ID}/extra',
    f'https://youtu.be//{VIDEO_ID}',
])
def test_parse_target_rejects_short_links_with_other_paths(url):
    """A youtu.be link must have exactly one path segment."""
    with pytest.raises(testee.UnsupportedUrlError, match='youtu.be'):
        testee.parse_target(url)


@pytest.mark.parametrize('url', [
    f'https://www.youtube.com/shorts/{VIDEO_ID}/extra',
    f'https://www.youtube.com/embed/{VIDEO_ID}',
    f'https://www.youtube.com//watch?v={VIDEO_ID}',
    f'https://www.youtube.com/Watch?v={VIDEO_ID}',
    'https://www.youtube.com/',
    'https://www.youtube.com',
    'https://www.youtube.com/feed/subscriptions',
])
def test_parse_target_rejects_other_paths(url):
    """Paths other than the supported forms are rejected with a generic reason."""
    with pytest.raises(testee.UnsupportedUrlError, match='Not a supported'):
        testee.parse_target(url)


@pytest.mark.parametrize('url', [
    f'https://www.youtube.com:443/watch?v={VIDEO_ID}',
    f'https://user@www.youtube.com/watch?v={VIDEO_ID}',
    f'https://music.youtube.com/watch?v={VIDEO_ID}',
    f'https://www.youtube-nocookie.com/embed/{VIDEO_ID}',
    f'https://youtube.com.example.org/watch?v={VIDEO_ID}',
    f'https://www.youtube.com./watch?v={VIDEO_ID}',
    f'https://www.youtu.be/{VIDEO_ID}',
])
def test_parse_target_rejects_other_hosts_ports_and_user_information(url):
    """Only the exact supported hosts are accepted, without ports or user information."""
    with pytest.raises(testee.UnsupportedUrlError, match='Only youtube.com'):
        testee.parse_target(url)


@pytest.mark.parametrize('url', [
    f'ftp://www.youtube.com/watch?v={VIDEO_ID}', f'www.youtube.com/watch?v={VIDEO_ID}', '', 'not a url',
])
def test_parse_target_rejects_other_schemes(url):
    """URLs without an http or https scheme are rejected."""
    with pytest.raises(testee.UnsupportedUrlError, match='http and https'):
        testee.parse_target(url)


def test_parse_target_rejects_unparsable_urls():
    """A URL that urllib cannot split is reported as malformed."""
    with pytest.raises(testee.UnsupportedUrlError, match='Malformed'):
        testee.parse_target('https://[www.youtube.com/watch')
