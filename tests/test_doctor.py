"""doctor's parsers, against authentic command output.

These parsers exist to catch the settings that silently stop a PC waking.
That makes a *wrong* parser worse than no check at all: a false OK sends
someone away satisfied while their PC never wakes. None of these commands
can be run on the test machine, so real captured output is the only way to
know the parsing is right.
"""

from unittest import mock

import pytest

from pcwake.agent import doctor
from pcwake.agent.doctor import Result

# --- authentic command output -------------------------------------------

ETHTOOL_WOL_DISABLED = """Settings for eth0:
\tSupported ports: [ TP MII ]
\tSupports auto-negotiation: Yes
\tSpeed: 1000Mb/s
\tDuplex: Full
\tPort: MII
\tPHYAD: 1
\tTransceiver: internal
\tSupports Wake-on: pumbg
\tWake-on: d
\tCurrent message level: 0x00000033 (51)
\tLink detected: yes
"""

ETHTOOL_WOL_ENABLED = ETHTOOL_WOL_DISABLED.replace("\tWake-on: d", "\tWake-on: g")

ETHTOOL_NO_WOL_SUPPORT = """Settings for eth0:
\tSupports auto-negotiation: Yes
\tLink detected: yes
"""

# powercfg /a on a desktop with hibernation left enabled
POWERCFG_HIBERNATE_ON = """The following sleep states are available on this system:
    Standby (S3)
    Hibernate
    Hybrid Sleep
    Fast Startup

The following sleep states are not available on this system:
    Standby (S1)
        The system firmware does not support this standby state.
    Standby (S2)
        The system firmware does not support this standby state.
"""

POWERCFG_HIBERNATE_OFF = """The following sleep states are available on this system:
    Standby (S3)

The following sleep states are not available on this system:
    Standby (S1)
        The system firmware does not support this standby state.
    Hibernate
        Hibernation has not been enabled.
    Hybrid Sleep
        Hibernation is not available.
    Fast Startup
        Hibernation is not available.
"""

POWERCFG_NO_SLEEP_AT_ALL = """The following sleep states are not available on this system:
    Standby (S1)
        The system firmware does not support this standby state.
    Standby (S2)
        The system firmware does not support this standby state.
    Standby (S3)
        The system firmware does not support this standby state.
    Hibernate
        Hibernation has not been enabled.
"""

REG_FAST_STARTUP_ON = """
HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power
    HiberbootEnabled    REG_DWORD    0x1

"""

REG_FAST_STARTUP_OFF = REG_FAST_STARTUP_ON.replace("0x1", "0x0")

WAKE_ARMED_NONE = "NONE\n"
WAKE_ARMED_NIC = "Realtek PCIe GbE Family Controller\nHID Keyboard Device\n"

IP_ROUTE = "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.50 uid 0 \n    cache \n"


def run_returns(output: str, code: int = 0):
    return mock.patch.object(doctor, "_run", return_value=(code, output))


class TestParseWakeOn:
    def test_reads_the_current_setting_not_the_supported_modes(self):
        """The bug this test exists for.

        ethtool prints both `Supports Wake-on: pumbg` and `Wake-on: d`. A
        substring search finds the first, which nearly always contains "g" --
        so doctor reported that waking would work on a machine where it was
        switched off, in the exact check meant to catch that.
        """
        assert doctor._parse_wake_on(ETHTOOL_WOL_DISABLED) == "d"

    def test_reads_an_enabled_setting(self):
        assert doctor._parse_wake_on(ETHTOOL_WOL_ENABLED) == "g"

    def test_returns_nothing_when_absent(self):
        assert doctor._parse_wake_on(ETHTOOL_NO_WOL_SUPPORT) is None

    @pytest.mark.parametrize("value", ["g", "pg", "ug", "umbg", "pumbg"])
    def test_any_setting_including_g_is_read_intact(self, value):
        output = ETHTOOL_WOL_DISABLED.replace("\tWake-on: d", f"\tWake-on: {value}")
        assert doctor._parse_wake_on(output) == value


class TestLinuxWol:
    def test_disabled_wol_is_a_failure_with_the_fix_command(self):
        with mock.patch.object(doctor, "_default_interface", return_value="eth0"), \
             run_returns(ETHTOOL_WOL_DISABLED):
            check = doctor.check_linux_wol()
        assert check.result is Result.FAIL
        assert "Wake-on: d" in check.detail
        assert "ethtool -s eth0 wol g" in check.fix

    def test_enabled_wol_passes(self):
        with mock.patch.object(doctor, "_default_interface", return_value="eth0"), \
             run_returns(ETHTOOL_WOL_ENABLED):
            check = doctor.check_linux_wol()
        assert check.result is Result.OK

    def test_wifi_is_flagged_before_ethtool_is_even_run(self):
        # WoWLAN is unreliable on most hardware; saying so is more useful
        # than reporting on a setting that will not save them.
        with mock.patch.object(doctor, "_default_interface", return_value="wlan0"):
            check = doctor.check_linux_wol()
        assert check.result is Result.WARN
        assert "Wi-Fi" in check.detail

    def test_ethtool_needing_root_warns_rather_than_failing(self):
        # Not knowing is different from knowing it is wrong.
        with mock.patch.object(doctor, "_default_interface", return_value="eth0"), \
             run_returns("Operation not permitted", code=1):
            check = doctor.check_linux_wol()
        assert check.result is Result.WARN

    def test_an_unknown_interface_warns(self):
        with mock.patch.object(doctor, "_default_interface", return_value=None):
            assert doctor.check_linux_wol().result is Result.WARN


class TestDefaultInterface:
    def test_extracts_the_device_from_ip_route(self):
        with run_returns(IP_ROUTE):
            assert doctor._default_interface() == "eth0"

    def test_returns_nothing_when_ip_route_fails(self):
        with run_returns("", code=1):
            assert doctor._default_interface() is None


class TestWindowsFastStartup:
    def test_enabled_fast_startup_fails_loudly(self):
        # The single most common reason a shut-down PC will not wake.
        with run_returns(REG_FAST_STARTUP_ON):
            check = doctor.check_windows_fast_startup()
        assert check.result is Result.FAIL
        assert "powercfg /h off" in check.fix

    def test_disabled_fast_startup_passes(self):
        with run_returns(REG_FAST_STARTUP_OFF):
            assert doctor.check_windows_fast_startup().result is Result.OK

    def test_an_unreadable_key_warns(self):
        with run_returns("ERROR: The system was unable to find the key", code=1):
            assert doctor.check_windows_fast_startup().result is Result.WARN

    def test_unparseable_output_warns_rather_than_guessing(self):
        with run_returns("something entirely unexpected"):
            assert doctor.check_windows_fast_startup().result is Result.WARN


class TestWindowsHibernation:
    def test_hibernation_enabled_warns_that_sleep_will_hibernate(self):
        with run_returns(POWERCFG_HIBERNATE_ON):
            check = doctor.check_windows_hibernation()
        assert check.result is Result.WARN
        assert "hibernate" in check.detail.lower()

    def test_hibernation_off_with_standby_available_passes(self):
        with run_returns(POWERCFG_HIBERNATE_OFF):
            assert doctor.check_windows_hibernation().result is Result.OK

    def test_the_unavailable_section_is_not_mistaken_for_the_available_one(self):
        # "Hibernate" appears in the not-available list too. Reading the
        # whole output would report hibernation enabled when it is off.
        with run_returns(POWERCFG_HIBERNATE_OFF):
            check = doctor.check_windows_hibernation()
        assert check.result is Result.OK, check.detail

    def test_no_sleep_state_at_all_is_a_failure(self):
        with run_returns(POWERCFG_NO_SLEEP_AT_ALL):
            check = doctor.check_windows_hibernation()
        assert check.result is Result.FAIL
        assert "standby" in check.detail.lower()


class TestWindowsWakeArmed:
    def test_no_armed_device_fails_with_the_device_manager_route(self):
        with run_returns(WAKE_ARMED_NONE):
            check = doctor.check_windows_wake_armed()
        assert check.result is Result.FAIL
        assert "Device Manager" in check.fix

    def test_an_armed_nic_passes_and_names_it(self):
        with run_returns(WAKE_ARMED_NIC):
            check = doctor.check_windows_wake_armed()
        assert check.result is Result.OK
        assert "Realtek" in check.detail


class TestReport:
    def test_a_failure_exits_nonzero(self):
        checks = [doctor.Check("x", Result.FAIL, "broken")]
        assert doctor.report(checks) == 1

    def test_warnings_alone_still_exit_zero(self):
        # Warnings are advisory; failing the command on them would make the
        # exit code useless for scripting.
        checks = [doctor.Check("x", Result.WARN, "unsure")]
        assert doctor.report(checks) == 0

    def test_all_ok_exits_zero(self):
        assert doctor.report([doctor.Check("x", Result.OK, "fine")]) == 0

    def test_a_failing_check_renders_its_fix(self):
        check = doctor.Check("x", Result.FAIL, "broken", fix="do the thing")
        assert "do the thing" in check.render()

    def test_a_passing_check_does_not_nag_with_a_fix(self):
        check = doctor.Check("x", Result.OK, "fine", fix="do the thing")
        assert "do the thing" not in check.render()
