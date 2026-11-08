"""Tests for the response models (DESIGN section 6)."""

import pytest
from pydantic import ValidationError

import youtube_collector.responses as testee


def test_models_refuse_fields_they_do_not_declare():
    """A record that gains a field must gain it in its model too, so nothing goes undocumented."""
    with pytest.raises(ValidationError, match='Extra inputs'):
        testee.FailureList(failures=[], revision=1)


def test_models_refuse_values_outside_their_enums():
    """Published enums are the real sets of values."""
    with pytest.raises(ValidationError):
        testee._Target(type='channel', id='x')
