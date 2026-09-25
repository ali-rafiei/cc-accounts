from datetime import datetime, timedelta

import pytest

import usage_table


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    for name in ('personal', 'work', 'bin', '_scratch', '.hidden'):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(usage_table, 'PROFILES_DIR', tmp_path)
    emails = {
        None: 'personal@example.com',
        tmp_path / 'personal': 'personal@example.com',
        tmp_path / 'work': 'work@example.com',
    }
    monkeypatch.setattr(usage_table, '_account_email', emails.get)
    return tmp_path


def test__discover__lists_only_account_profiles(profiles):
    # Act
    names = [name for name, _ in usage_table._discover()]

    # Assert
    assert names == ['personal', 'work']


def test__discover__adds_a_default_row_for_an_account_no_profile_covers(profiles, monkeypatch):
    # Arrange
    emails = {
        None: 'someone-else@example.com',
        profiles / 'personal': 'personal@example.com',
        profiles / 'work': 'work@example.com',
    }
    monkeypatch.setattr(usage_table, '_account_email', emails.get)

    # Act
    names = [name for name, _ in usage_table._discover()]

    # Assert
    assert names == ['default', 'personal', 'work']


def test__discover__probes_the_loaded_profile_through_the_default_login(profiles):
    # Arrange: cc-use put work's login in the default slot, so its own copy may be stale.
    (profiles / '.loaded').write_text('work\n')

    # Act
    config_dirs = dict(usage_table._discover())

    # Assert
    assert config_dirs['work'] is None
    assert config_dirs['personal'] == profiles / 'personal'


def test__parse_reset__resolves_a_date_to_the_coming_weekday():
    # Arrange
    target = (datetime.now() + timedelta(days=30)).replace(hour=15, minute=0, second=0, microsecond=0)

    # Act
    resolved, label = usage_table._parse_reset(f'{target:%b} {target.day} at 3pm')

    # Assert
    assert resolved is not None
    assert resolved == target
    assert label == f'{resolved.strftime("%A")} at 3pm'


def test__parse_reset__leaves_an_unrecognised_string_alone():
    # Act / Assert
    assert usage_table._parse_reset('soon') == (None, 'soon')
