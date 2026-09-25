import locale
from datetime import datetime, timedelta

import pytest

import usage_table


@pytest.fixture(autouse=True)
def isolate_machine(tmp_path_factory, monkeypatch):
    # No test may reach the real claude or the real ~/.claude.json, even through a bug.
    machine = tmp_path_factory.mktemp('machine')
    (machine / 'empty-bin').mkdir()
    monkeypatch.setenv('PATH', str(machine / 'empty-bin'))
    monkeypatch.setenv('HOME', str(machine))
    monkeypatch.setenv('USERPROFILE', str(machine))
    monkeypatch.setattr(usage_table, 'PROFILES_DIR', machine / '.claude-profiles')


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


def test__discover__probes_a_loaded_name_with_edge_spaces_through_the_default_login(profiles):
    # Arrange: cc-use records the folder name exactly, spaces and all.
    (profiles / 'work ').mkdir()
    (profiles / '.loaded').write_text('work \n')

    # Act
    config_dirs = dict(usage_table._discover())

    # Assert
    assert config_dirs['work '] is None


def test__parse_reset__resolves_a_date_to_the_coming_weekday():
    # Arrange
    target = (datetime.now() + timedelta(days=30)).replace(hour=15, minute=0, second=0, microsecond=0)

    # Act
    resolved, label = usage_table._parse_reset(target.strftime('%b %-d at 3pm'))

    # Assert
    assert resolved is not None
    assert resolved == target
    assert label == f'{resolved.strftime("%A")} at 3pm'


def test__parse_reset__leaves_an_unrecognised_string_alone():
    # Act / Assert
    assert usage_table._parse_reset('soon') == (None, 'soon')


def _frozen_now(monkeypatch, moment):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(usage_table, 'datetime', FrozenDatetime)


def _fake_claude(monkeypatch, auth_status, usage=''):
    def fake(args, config_dir):
        return auth_status if args[:2] == ['auth', 'status'] else usage

    monkeypatch.setattr(usage_table, '_claude', fake)


def test__probe__keeps_a_week_reset_with_no_timezone_to_its_own_line(profiles, monkeypatch):
    # Arrange: no '(zone)' suffix, so nothing but the line end bounds the reset text.
    _frozen_now(monkeypatch, datetime(2026, 9, 25, 9, 0))
    usage = (
        'Current session: 50% used · resets Sep 25 at 2pm\n'
        'Current week (all models): 9% used · resets Oct 2 at 7am\n'
        'Current week (Fable): 5% used · resets Oct 2 at 7am\n'
        '\n'
        "What's contributing to your limits usage?\n"
    )
    _fake_claude(monkeypatch, '{"loggedIn": true}', usage)

    # Act
    row = usage_table._probe('work', profiles / 'work')

    # Assert
    assert row['reset_at'] == datetime(2026, 10, 2, 7, 0)
    assert row['resets'] == 'Friday at 7am'


def test__logged_in__reads_the_json_after_a_warning_line():
    # Arrange
    status = 'Warning: a newer version is available\n{"loggedIn": true, "authMethod": "claude.ai"}\n'

    # Act
    logged_in = usage_table._logged_in(status)

    # Assert
    assert logged_in is True


def test__probe__shows_why_claude_could_not_run_instead_of_not_logged_in(profiles, monkeypatch):
    # Arrange
    _fake_claude(monkeypatch, 'error: claude is not on PATH')

    # Act
    row = usage_table._probe('work', profiles / 'work')

    # Assert
    assert row['note'] == 'error: claude is not on PATH'


def test__current_account__falls_back_to_the_default_login_when_the_loaded_profile_is_gone(profiles, monkeypatch):
    # Arrange: .loaded still names a profile whose directory was deleted.
    (profiles / '.loaded').write_text('deleted\n')
    monkeypatch.delenv('CLAUDE_CONFIG_DIR', raising=False)

    # Act
    current = usage_table._current_account()

    # Assert
    assert current == 'personal@example.com'


def test__parse_reset__reads_a_date_that_carries_its_year(monkeypatch):
    # Arrange
    _frozen_now(monkeypatch, datetime(2026, 9, 25, 9, 0))

    # Act
    resolved, label = usage_table._parse_reset('Sep 26, 2026 at 3am')

    # Assert
    assert resolved == datetime(2026, 9, 26, 3, 0)
    assert label == 'Saturday at 3am'


def test__parse_reset__reads_a_time_only_reset_as_today(monkeypatch):
    # Arrange
    _frozen_now(monkeypatch, datetime(2026, 9, 25, 9, 0))

    # Act
    resolved, label = usage_table._parse_reset('3pm')

    # Assert
    assert resolved == datetime(2026, 9, 25, 15, 0)
    assert label == 'Friday at 3pm'


def test__parse_reset__puts_a_just_passed_new_year_reset_in_the_old_year(monkeypatch):
    # Arrange: early January, and the reset shown is a few days ago in December.
    _frozen_now(monkeypatch, datetime(2027, 1, 3, 9, 0))

    # Act
    resolved, _ = usage_table._parse_reset('Dec 31 at 11pm')

    # Assert
    assert resolved == datetime(2026, 12, 31, 23, 0)


def test__account_email__treats_a_null_oauth_account_as_no_email(tmp_path):
    # Arrange
    (tmp_path / '.claude.json').write_text('{"oauthAccount": null}')

    # Act
    email = usage_table._account_email(tmp_path)

    # Assert
    assert email is None


def test__probe__shows_a_usage_timeout_instead_of_no_limit_data(profiles, monkeypatch):
    # Arrange
    timeout = "error: Command '['claude', '-p', '/usage']' timed out after 120 seconds"
    _fake_claude(monkeypatch, '{"loggedIn": true}', timeout)

    # Act
    row = usage_table._probe('work', profiles / 'work')

    # Assert
    assert row['note'].startswith('error: ') and 'timed out' in row['note']


def test__parse_reset__reads_feb_29_in_a_leap_year(monkeypatch):
    # Arrange
    _frozen_now(monkeypatch, datetime(2028, 2, 25, 9, 0))

    # Act
    resolved, _ = usage_table._parse_reset('Feb 29 at 3pm')

    # Assert
    assert resolved == datetime(2028, 2, 29, 15, 0)


def test__claude__decodes_utf8_output_under_an_ascii_locale(tmp_path, monkeypatch):
    # Arrange: a stand-in claude that prints the real output's middle dot.
    fake = tmp_path / 'claude'
    fake.write_text("#!/bin/sh\nprintf 'Current week (all models): 9%% used \\302\\267 resets Oct 2 at 7am\\n'\n")
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setattr(locale, 'getpreferredencoding', lambda do_setlocale=True: 'ascii')
    monkeypatch.setattr(locale, 'getencoding', lambda: 'ascii', raising=False)  # what subprocess reads on 3.11+

    # Act
    out = usage_table._claude(['-p', '/usage'], None)

    # Assert
    assert out == 'Current week (all models): 9% used \u00b7 resets Oct 2 at 7am\n'


def test__claude__reports_a_claude_it_cannot_execute(tmp_path, monkeypatch):
    # Arrange: a claude on PATH without the execute bit.
    (tmp_path / 'claude').write_text('not a program')
    monkeypatch.setenv('PATH', str(tmp_path))

    # Act
    out = usage_table._claude(['auth', 'status'], None)

    # Assert
    assert out.startswith('error: ')
