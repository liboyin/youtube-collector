"""Validate caption language candidates and choose one caption track (DESIGN 5.6).

Candidates are well-formed BCP 47 tags in the caller's order of preference. A candidate matches a
track whose language code begins with the candidate's subtags (RFC 4647 basic filtering), with no
fallback to shorter tags. Manual tracks win across the whole candidate list.
"""

import re
from collections.abc import Sequence
from typing import Protocol

__all__ = ['CaptionTrack', 'is_well_formed', 'select_track']

# RFC 5646 section 2.1, Language-Tag = langtag / privateuse / grandfathered. Matched
# case-insensitively; well-formedness only, so registry validity and duplicate subtags are not checked.
_LANGTAG = r"""
    (?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4}|[a-z]{5,8})   # language, with up to three extlangs
    (?:-[a-z]{4})?                                         # script
    (?:-(?:[a-z]{2}|[0-9]{3}))?                            # region
    (?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*               # variants
    (?:-[0-9a-wyz](?:-[a-z0-9]{2,8})+)*                    # extensions; the singleton is not x
    (?:-x(?:-[a-z0-9]{1,8})+)?                             # private use
"""
_PRIVATEUSE = r'x(?:-[a-z0-9]{1,8})+'
_GRANDFATHERED = (
    'en-gb-oed|i-ami|i-bnn|i-default|i-enochian|i-hak|i-klingon|i-lux|i-mingo|i-navajo|i-pwn|i-tao|i-tay|i-tsu'
    '|sgn-be-fr|sgn-be-nl|sgn-ch-de|art-lojban|cel-gaulish|no-bok|no-nyn|zh-guoyu|zh-hakka|zh-min|zh-min-nan|zh-xiang'
)
_LANGUAGE_TAG = re.compile(
    rf'(?:{_GRANDFATHERED})|(?:{_PRIVATEUSE})|(?:{_LANGTAG})', re.IGNORECASE | re.VERBOSE | re.ASCII,
)


class CaptionTrack(Protocol):
    """A listed caption track, as ``youtube-transcript-api``'s ``Transcript`` describes one."""

    @property
    def language_code(self) -> str:
        """YouTube's language code for the track, such as ``zh-Hans``."""
        ...

    @property
    def is_generated(self) -> bool:
        """Whether YouTube generated the track automatically rather than a person creating it."""
        ...


def is_well_formed(tag: str) -> bool:
    """Return whether ``tag`` is a well-formed BCP 47 language tag.

    Any tag the RFC 5646 grammar accepts is allowed; there is no list of supported languages.
    """
    return _LANGUAGE_TAG.fullmatch(tag) is not None


def select_track[T: CaptionTrack](candidates: Sequence[str], tracks: Sequence[T]) -> T | None:
    """Choose the caption track for an ordered list of language candidates.

    Candidates are tried in order against manual tracks, then in order against generated tracks.
    For one candidate and one kind, an exact (case-insensitive) match wins, then the order in
    which YouTube listed the tracks. Only the given tracks are considered, so callers pass the
    listed tracks and never translations.

    Args:
        candidates: Language candidates in order of preference.
        tracks: The tracks YouTube lists for the video, in its order.

    Returns:
        The chosen track, or None when no candidate matches any track.
    """
    for generated in (False, True):
        kind = [track for track in tracks if bool(track.is_generated) == generated]
        for candidate in candidates:
            matches = [track for track in kind if _matches(candidate, track.language_code)]
            if matches:
                exact = [track for track in matches if track.language_code.lower() == candidate.lower()]
                return (exact or matches)[0]
    return None


def _matches(candidate: str, code: str) -> bool:
    """Return whether the candidate's subtags are a case-insensitive prefix of the code's subtags."""
    candidate_subtags = candidate.lower().split('-')
    code_subtags = code.lower().split('-')
    return code_subtags[:len(candidate_subtags)] == candidate_subtags
