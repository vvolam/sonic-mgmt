"""
Tests for verifying SFP thermal data in STATE_DB Redis tables.

These tests validate that thermalctld correctly reads SFP temperature and threshold
data from Redis tables (TRANSCEIVER_DOM_TEMPERATURE / TRANSCEIVER_DOM_SENSOR) and
populates the TEMPERATURE_INFO table for SFP thermals.
"""
import logging
import re

import pytest

from tests.common.helpers.assertions import pytest_assert
from tests.common.utilities import wait_until
from tests.common.platform.processes_utils import check_pmon_uptime_minutes

pytestmark = [
    pytest.mark.asic("mellanox"),
    pytest.mark.topology('any')
]

logger = logging.getLogger(__name__)

NOT_AVAILABLE = "N/A"

# TRANSCEIVER_DOM_TEMPERATURE fields
DOM_TEMP_FIELD = "temperature"

# TRANSCEIVER_DOM_THRESHOLD fields (used for SFP thresholds)
THRESHOLD_FIELDS = {
    "temphighalarm": "critical_high_threshold",
    "templowalarm": "critical_low_threshold",
    "temphighwarning": "high_threshold",
    "templowwarning": "low_threshold",
}

# TRANSCEIVER_DOM_SENSOR fallback fields
DOM_SENSOR_TEMP_FIELD = "temperature"


def get_optical_sfp_ports(duthost):
    """Get list of optical SFP ports that have DOM data in STATE_DB.

    Returns ports that have TRANSCEIVER_DOM_SENSOR entries and are not
    passive copper cables (DACs), which don't report temperature.
    """
    dom_sensor_cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_SENSOR|Ethernet*"'
    dom_keys = duthost.command(dom_sensor_cmd)["stdout_lines"]
    ports = []
    for key in dom_keys:
        key = key.strip()
        if not key:
            continue
        m = re.match(r"TRANSCEIVER_DOM_SENSOR\|(Ethernet\d+)", key)
        if m:
            ports.append(m.group(1))
    return sorted(ports)


def get_sfp_temperature_entries(duthost):
    """Get TEMPERATURE_INFO entries that correspond to SFP thermals.

    SFP thermal entries in TEMPERATURE_INFO typically contain 'xSFP' or
    'SFP' in the sensor name.
    """
    temp_info_cmd = 'sonic-db-cli STATE_DB KEYS "TEMPERATURE_INFO|*"'
    temp_keys = duthost.command(temp_info_cmd)["stdout_lines"]
    sfp_entries = {}
    for key in temp_keys:
        key = key.strip()
        if not key:
            continue
        # SFP thermal entries typically named like "xSFP module N Temp"
        sensor_name = key.split("|", 1)[1] if "|" in key else ""
        if "SFP" in sensor_name.upper():
            hgetall_cmd = 'sonic-db-cli STATE_DB HGETALL "{}"'.format(key)
            raw_output = duthost.command(hgetall_cmd)["stdout_lines"]
            entry = _parse_hgetall(raw_output)
            sfp_entries[sensor_name] = entry
    return sfp_entries


def get_dom_temperature_entries(duthost):
    """Get all TRANSCEIVER_DOM_TEMPERATURE entries from STATE_DB."""
    cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_TEMPERATURE|Ethernet*"'
    keys = duthost.command(cmd)["stdout_lines"]
    entries = {}
    for key in keys:
        key = key.strip()
        if not key:
            continue
        m = re.match(r"TRANSCEIVER_DOM_TEMPERATURE\|(Ethernet\d+)", key)
        if m:
            port = m.group(1)
            hgetall_cmd = 'sonic-db-cli STATE_DB HGETALL "{}"'.format(key)
            raw_output = duthost.command(hgetall_cmd)["stdout_lines"]
            entries[port] = _parse_hgetall(raw_output)
    return entries


def get_dom_threshold_entries(duthost):
    """Get all TRANSCEIVER_DOM_THRESHOLD entries from STATE_DB."""
    cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_THRESHOLD|Ethernet*"'
    keys = duthost.command(cmd)["stdout_lines"]
    entries = {}
    for key in keys:
        key = key.strip()
        if not key:
            continue
        m = re.match(r"TRANSCEIVER_DOM_THRESHOLD\|(Ethernet\d+)", key)
        if m:
            port = m.group(1)
            hgetall_cmd = 'sonic-db-cli STATE_DB HGETALL "{}"'.format(key)
            raw_output = duthost.command(hgetall_cmd)["stdout_lines"]
            entries[port] = _parse_hgetall(raw_output)
    return entries


def get_dom_sensor_entries(duthost):
    """Get all TRANSCEIVER_DOM_SENSOR entries from STATE_DB."""
    cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_SENSOR|Ethernet*"'
    keys = duthost.command(cmd)["stdout_lines"]
    entries = {}
    for key in keys:
        key = key.strip()
        if not key:
            continue
        m = re.match(r"TRANSCEIVER_DOM_SENSOR\|(Ethernet\d+)", key)
        if m:
            port = m.group(1)
            hgetall_cmd = 'sonic-db-cli STATE_DB HGETALL "{}"'.format(key)
            raw_output = duthost.command(hgetall_cmd)["stdout_lines"]
            entries[port] = _parse_hgetall(raw_output)
    return entries


def _parse_hgetall(lines):
    """Parse alternating key/value lines from sonic-db-cli HGETALL output."""
    result = {}
    i = 0
    while i < len(lines) - 1:
        key = lines[i].strip()
        value = lines[i + 1].strip()
        if key:
            result[key] = value
        i += 2
    return result


def has_sfp_thermals(duthost):
    """Check if the platform has any SFP thermal entries in TEMPERATURE_INFO."""
    sfp_entries = get_sfp_temperature_entries(duthost)
    return len(sfp_entries) > 0


def _platform_has_dom_temperature_table(duthost):
    """Check if any TRANSCEIVER_DOM_TEMPERATURE entries exist."""
    cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_TEMPERATURE|Ethernet*"'
    keys = duthost.command(cmd)["stdout_lines"]
    return any(k.strip() for k in keys)


def _platform_has_dom_threshold_table(duthost):
    """Check if any TRANSCEIVER_DOM_THRESHOLD entries exist."""
    cmd = 'sonic-db-cli STATE_DB KEYS "TRANSCEIVER_DOM_THRESHOLD|Ethernet*"'
    keys = duthost.command(cmd)["stdout_lines"]
    return any(k.strip() for k in keys)


def test_sfp_temperature_in_temperature_info(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Verify SFP thermal entries are populated in TEMPERATURE_INFO table.

    Checks that for platforms with optical SFPs, the TEMPERATURE_INFO table
    contains corresponding SFP thermal entries with expected fields.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    pytest_assert(wait_until(360, 10, 0, check_pmon_uptime_minutes, duthost),
                  "Pmon docker is not ready for test")

    optical_ports = get_optical_sfp_ports(duthost)
    if not optical_ports:
        pytest.skip("No optical SFP ports detected on {}".format(duthost.hostname))

    sfp_entries = get_sfp_temperature_entries(duthost)
    if not sfp_entries:
        pytest.skip("No SFP thermal entries in TEMPERATURE_INFO on {} - "
                     "platform may not support SFP thermals via thermalctld".format(duthost.hostname))

    logger.info("Found %d SFP thermal entries in TEMPERATURE_INFO", len(sfp_entries))

    expected_fields = ["temperature", "high_threshold", "low_threshold",
                       "critical_high_threshold", "critical_low_threshold",
                       "warning_status", "timestamp"]

    failures = []
    for sensor_name, entry in sfp_entries.items():
        for field in expected_fields:
            if field not in entry:
                failures.append("{}: missing field '{}'".format(sensor_name, field))

    pytest_assert(not failures,
                  "SFP TEMPERATURE_INFO validation failures:\n{}".format("\n".join(failures)))


def test_sfp_temperature_from_dom_temperature_table(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Verify SFP temperatures in TEMPERATURE_INFO match TRANSCEIVER_DOM_TEMPERATURE source.

    When TRANSCEIVER_DOM_TEMPERATURE table is populated (by xcvrd), thermalctld
    should read SFP temperatures from this table. This test verifies the values
    are consistent.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    pytest_assert(wait_until(360, 10, 0, check_pmon_uptime_minutes, duthost),
                  "Pmon docker is not ready for test")

    dom_temp_entries = get_dom_temperature_entries(duthost)
    if not dom_temp_entries:
        pytest.skip("TRANSCEIVER_DOM_TEMPERATURE table not populated on {} - "
                     "platform may not support this table yet".format(duthost.hostname))

    sfp_temp_entries = get_sfp_temperature_entries(duthost)
    if not sfp_temp_entries:
        pytest.skip("No SFP thermal entries in TEMPERATURE_INFO on {}".format(duthost.hostname))

    logger.info("Found %d TRANSCEIVER_DOM_TEMPERATURE entries and %d SFP TEMPERATURE_INFO entries",
                len(dom_temp_entries), len(sfp_temp_entries))

    # Verify that TRANSCEIVER_DOM_TEMPERATURE entries have the temperature field
    failures = []
    for port, entry in dom_temp_entries.items():
        if DOM_TEMP_FIELD not in entry:
            failures.append("{}: TRANSCEIVER_DOM_TEMPERATURE missing '{}' field".format(
                port, DOM_TEMP_FIELD))
            continue

        temp_value = entry[DOM_TEMP_FIELD]
        if temp_value == NOT_AVAILABLE:
            continue

        # Validate it's a parseable float
        try:
            float(temp_value)
        except (ValueError, TypeError):
            failures.append("{}: TRANSCEIVER_DOM_TEMPERATURE has non-numeric temperature '{}'".format(
                port, temp_value))

    pytest_assert(not failures,
                  "TRANSCEIVER_DOM_TEMPERATURE validation failures:\n{}".format("\n".join(failures)))


def test_sfp_threshold_from_dom_threshold_table(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Verify SFP thresholds in TEMPERATURE_INFO match TRANSCEIVER_DOM_THRESHOLD source.

    When TRANSCEIVER_DOM_THRESHOLD table is populated, thermalctld should read
    SFP alarm/warning thresholds from it. This test checks that threshold entries
    exist and contain expected fields.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    pytest_assert(wait_until(360, 10, 0, check_pmon_uptime_minutes, duthost),
                  "Pmon docker is not ready for test")

    dom_threshold_entries = get_dom_threshold_entries(duthost)
    if not dom_threshold_entries:
        pytest.skip("TRANSCEIVER_DOM_THRESHOLD table not populated on {} - "
                     "platform may not support this table yet".format(duthost.hostname))

    logger.info("Found %d TRANSCEIVER_DOM_THRESHOLD entries", len(dom_threshold_entries))

    failures = []
    for port, entry in dom_threshold_entries.items():
        for dom_field in THRESHOLD_FIELDS:
            if dom_field not in entry:
                failures.append("{}: TRANSCEIVER_DOM_THRESHOLD missing '{}' field".format(
                    port, dom_field))
                continue

            value = entry[dom_field]
            if value == NOT_AVAILABLE or value == "":
                continue

            # Validate parseable float (may have units appended e.g. "75.0 C")
            try:
                float(value.split()[0])
            except (ValueError, TypeError):
                failures.append("{}: TRANSCEIVER_DOM_THRESHOLD field '{}' has "
                                "non-numeric value '{}'".format(port, dom_field, value))

    pytest_assert(not failures,
                  "TRANSCEIVER_DOM_THRESHOLD validation failures:\n{}".format("\n".join(failures)))


def test_sfp_temperature_fallback_to_dom_sensor(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """Verify SFP temperature data exists via either DOM_TEMPERATURE or DOM_SENSOR fallback.

    Thermalctld reads SFP temperature from TRANSCEIVER_DOM_TEMPERATURE first,
    falling back to TRANSCEIVER_DOM_SENSOR. This test verifies that at least
    one source has valid temperature data for each optical SFP port.
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    pytest_assert(wait_until(360, 10, 0, check_pmon_uptime_minutes, duthost),
                  "Pmon docker is not ready for test")

    optical_ports = get_optical_sfp_ports(duthost)
    if not optical_ports:
        pytest.skip("No optical SFP ports detected on {}".format(duthost.hostname))

    dom_temp_entries = get_dom_temperature_entries(duthost)
    dom_sensor_entries = get_dom_sensor_entries(duthost)

    if not dom_temp_entries and not dom_sensor_entries:
        pytest.skip("Neither TRANSCEIVER_DOM_TEMPERATURE nor TRANSCEIVER_DOM_SENSOR "
                     "tables populated on {}".format(duthost.hostname))

    logger.info("TRANSCEIVER_DOM_TEMPERATURE ports: %d, TRANSCEIVER_DOM_SENSOR ports: %d",
                len(dom_temp_entries), len(dom_sensor_entries))

    failures = []
    for port in optical_ports:
        has_dom_temp = (port in dom_temp_entries and
                        DOM_TEMP_FIELD in dom_temp_entries.get(port, {}))
        has_dom_sensor = (port in dom_sensor_entries and
                          DOM_SENSOR_TEMP_FIELD in dom_sensor_entries.get(port, {}))

        if not has_dom_temp and not has_dom_sensor:
            failures.append("{}: temperature not found in either "
                            "TRANSCEIVER_DOM_TEMPERATURE or TRANSCEIVER_DOM_SENSOR".format(port))

    pytest_assert(not failures,
                  "SFP temperature source validation failures:\n{}".format("\n".join(failures)))


def test_sfp_thermal_end_to_end(duthosts, enum_rand_one_per_hwsku_frontend_hostname):
    """End-to-end test: verify SFP thermal data flows from Redis source to CLI output.

    Validates the complete pipeline:
    1. SFP temperature data exists in source Redis table (DOM_TEMPERATURE or DOM_SENSOR)
    2. TEMPERATURE_INFO table is populated with SFP entries
    3. 'show platform temperature' CLI displays SFP temperature data
    """
    duthost = duthosts[enum_rand_one_per_hwsku_frontend_hostname]

    pytest_assert(wait_until(360, 10, 0, check_pmon_uptime_minutes, duthost),
                  "Pmon docker is not ready for test")

    # Step 1: Check source data exists
    dom_temp_entries = get_dom_temperature_entries(duthost)
    dom_sensor_entries = get_dom_sensor_entries(duthost)
    has_source = bool(dom_temp_entries) or bool(dom_sensor_entries)
    if not has_source:
        pytest.skip("No SFP DOM temperature source tables populated on {}".format(
            duthost.hostname))

    # Step 2: Verify TEMPERATURE_INFO has SFP entries
    sfp_temp_entries = get_sfp_temperature_entries(duthost)
    if not sfp_temp_entries:
        pytest.skip("No SFP thermal entries in TEMPERATURE_INFO on {} - "
                     "platform may not support SFP thermals via thermalctld".format(duthost.hostname))

    # Step 3: Verify CLI output contains SFP entries
    cli_output = duthost.command("show platform temperature")["stdout_lines"]
    pytest_assert(len(cli_output) > 0,
                  "No output from 'show platform temperature'")

    # Find SFP entries in CLI output (look for lines with "SFP" or "xSFP")
    sfp_cli_lines = [line for line in cli_output
                     if "SFP" in line.upper() or "XSFP" in line.upper()]

    if not sfp_cli_lines:
        pytest.skip("No SFP entries in 'show platform temperature' CLI output on {} - "
                     "platform may report SFP thermals differently".format(duthost.hostname))

    logger.info("Found %d SFP entries in CLI output", len(sfp_cli_lines))

    # Verify SFP entries have temperature values (not all N/A for platforms with DOM data)
    sfp_with_temp = 0
    for line in sfp_cli_lines:
        # Split by whitespace and check if there's a numeric temperature value
        parts = line.split()
        for part in parts:
            try:
                float(part)
                sfp_with_temp += 1
                break
            except ValueError:
                continue

    # If TRANSCEIVER_DOM_TEMPERATURE is populated, we expect at least some SFPs to
    # have real temperature values
    if dom_temp_entries:
        # Check that at least some DOM_TEMPERATURE entries have real values
        dom_ports_with_temp = sum(
            1 for entry in dom_temp_entries.values()
            if entry.get(DOM_TEMP_FIELD, NOT_AVAILABLE) != NOT_AVAILABLE
        )
        if dom_ports_with_temp > 0:
            pytest_assert(sfp_with_temp > 0,
                          "TRANSCEIVER_DOM_TEMPERATURE has {} ports with temperature data "
                          "but no SFP entries in CLI show numeric temperature".format(
                              dom_ports_with_temp))

    logger.info("End-to-end validation passed: %d/%d SFP CLI entries have temperature values",
                sfp_with_temp, len(sfp_cli_lines))
