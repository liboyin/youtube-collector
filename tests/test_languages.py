"""Tests for caption language validation and track selection (DESIGN 5.6)."""

from dataclasses import dataclass

import pytest

import youtube_collector.languages as testee


@dataclass(frozen=True)
class Track:
    """A listed caption track with the attributes the selector reads."""

    language_code: str
    is_generated: bool = False


def manual(code):
    """Return a manually created track."""
    return Track(code, False)


def generated(code):
    """Return an auto-generated track."""
    return Track(code, True)


@pytest.mark.parametrize('tag', [
    'en', 'EN', 'zh', 'zh-CN', 'zh-cn', 'zh-Hans', 'zh-Hant-TW', 'es-419', 'sr-Latn-RS', 'fil', 'yue', 'abcd',
    'abcdefgh', 'zh-yue', 'zh-yue-abc-def', 'de-CH-1901', 'sl-rozaj-biske', 'de-1996', 'en-US-u-ca-gregory',
    'en-a-bbb-x-a-ccc', 'en-x-private', 'x-whatever', 'X-A', 'i-klingon', 'zh-min-nan', 'en-GB-oed', 'art-lojban',
    'de-DE-1901-1901',  # duplicate variants are well-formed, though not valid
])
def test_is_well_formed_accepts_rfc_5646_tags(tag):
    """Tags matching the RFC 5646 grammar are accepted, whatever their case."""
    assert testee.is_well_formed(tag)


@pytest.mark.parametrize('tag', [
    '', 'e', 'abcdefghi', 'en_US', 'en-', '-en', 'en--US', 'en US', ' en', 'en\n', 'zh-yue-abc-def-ghi', 'en-x',
    'en-a', 'en-a-b', 'en-x-abcdefghi', 'x', 'en-US-1', 'en-12', 'de-CH-190', 'en-ÜS', '中文', 'en-x-a-',
    'i-unknown',
])
def test_is_well_formed_rejects_malformed_tags(tag):
    """Strings outside the grammar, including empty subtags and non-ASCII, are rejected."""
    assert not testee.is_well_formed(tag)


@pytest.mark.parametrize('code', ['zh', 'zh-TW', 'zh-CN', 'zh-Hans', 'zh-Hant'])
def test_select_track_matches_subtag_prefixes(code):
    """The candidate zh matches every zh track (DESIGN 5.6 rule 1)."""
    track = manual(code)
    assert testee.select_track(['zh'], [track]) is track


@pytest.mark.parametrize('code', ['zh-TW', 'zh-Hans', 'zh', 'zha', 'zh-CNX'])
def test_select_track_requires_every_candidate_subtag(code):
    """zh-cn matches neither another region, a script, plain zh, nor a longer subtag."""
    assert testee.select_track(['zh-cn'], [manual(code)]) is None


def test_select_track_respects_subtag_boundaries():
    """The candidate zh does not match zha: matching compares whole subtags, not string prefixes."""
    assert testee.select_track(['zh'], [manual('zha'), generated('zhx-Hans')]) is None


def test_select_track_matches_case_insensitively():
    """zh-cn matches a zh-CN track."""
    track = manual('zh-CN')
    assert testee.select_track(['zh-cn'], [track]) is track


def test_select_track_falls_back_only_to_later_candidates():
    """A shorter later candidate is the only way to reach plain zh (rule 2)."""
    plain = manual('zh')
    assert testee.select_track(['zh-cn'], [plain]) is None
    assert testee.select_track(['zh-cn', 'zh'], [plain]) is plain


def test_select_track_prefers_manual_tracks_across_the_whole_list():
    """A manual track for a later candidate beats a generated track for the first (rule 3)."""
    auto_first = generated('zh-CN')
    manual_later = manual('zh-Hans')
    assert testee.select_track(['zh-cn', 'zh'], [auto_first, manual_later]) is manual_later


def test_select_track_uses_generated_tracks_when_no_manual_track_matches():
    """Generated tracks are tried in candidate order once no manual track matches."""
    other_manual = manual('fr')
    second = generated('en')
    first = generated('zh-CN')
    assert testee.select_track(['zh', 'en'], [other_manual, second, first]) is first


def test_select_track_prefers_candidate_order_within_a_kind():
    """Within manual tracks, the earlier candidate wins over listed order."""
    english = manual('en')
    chinese = manual('zh')
    assert testee.select_track(['zh', 'en'], [english, chinese]) is chinese


def test_select_track_prefers_exact_match_then_listed_order():
    """For one candidate and kind, the exact code wins, then the first listed (rule 4)."""
    taiwan = manual('zh-TW')
    plain = manual('ZH')
    hans = manual('zh-Hans')
    assert testee.select_track(['zh'], [taiwan, hans, plain]) is plain
    assert testee.select_track(['zh'], [taiwan, hans]) is taiwan


def test_select_track_exact_match_does_not_cross_kinds():
    """An exact generated match does not beat a prefix manual match."""
    prefix_manual = manual('en-GB')
    assert testee.select_track(['en'], [generated('en'), prefix_manual]) is prefix_manual


def test_select_track_returns_none_without_tracks_or_candidates():
    """No tracks, or no candidates, select nothing."""
    assert testee.select_track(['en'], []) is None
    assert testee.select_track([], [manual('en')]) is None
