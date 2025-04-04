import re
import logging
from tests.common.platform.interface_utils import get_port_map


def parse_output(output_lines):
    """
    @summary: For parsing command output. The output lines should have format 'key value'.
    @param output_lines: Command output lines
    @return: Returns result in a dictionary
    """
    res = {}
    for line in output_lines:
        fields = line.split()
        if len(fields) < 2:
            continue
        res[fields[0]] = line.replace(fields[0], '').strip()
    return res


def parse_eeprom(output_lines):
    """
    @summary: Parse the SFP eeprom information from command output
    @param output_lines: Command output lines
    @return: Returns result in a dictionary
    """
    res = {}
    for line in output_lines:
        if re.match(r"^Ethernet\d+: .*", line):
            fields = line.split(":")
            res[fields[0]] = fields[1].strip()
    return res


def parse_eeprom_hexdump(data):
    # Define a regular expression to capture all required data
    regex = re.compile(
        r"EEPROM hexdump for port (\S+)\n"  # Capture port name
        r"(?:\s+)?"  # Match and skip intermediate lines
        r"((?:Lower|Upper) page \S+|\S+ dump)\n"  # Capture full page type string
        r"((?:\s+[0-9a-fA-F]{8}(?: [0-9a-fA-F]{2}){8} (?: [0-9a-fA-F]{2}){8} .*\n)+)"  # Capture hex data block
    )
    # Dictionary to store parsed results
    parsed_data = {}

    # Find all matches in the data
    matches = regex.findall(data)
    for port, page_type, hex_data in matches:
        if port not in parsed_data:
            parsed_data[port] = {}

        # Parse hex data block into individual hex values
        hex_lines = hex_data.splitlines()
        hex_values = [
            value
            for line in hex_lines
            for value in line[9:56].split()  # Extract hex bytes from columns 9-56
        ]

        parsed_data[port][page_type] = hex_values

    return parsed_data


def get_dev_conn(duthost, conn_graph_facts, asic_index):
    dev_conn = conn_graph_facts.get("device_conn", {}).get(duthost.hostname, {})

    # Get the interface pertaining to that asic
    portmap = get_port_map(duthost, asic_index)
    logging.info("Got portmap {}".format(portmap))

    if asic_index is not None:
        # Check if the interfaces of this AISC is present in conn_graph_facts
        dev_conn = {k: v for k, v in list(portmap.items()) if k in conn_graph_facts["device_conn"][duthost.hostname]}
        logging.info("ASIC {} interface_list {}".format(asic_index, dev_conn))

    return portmap, dev_conn


def validate_transceiver_lpmode(sfp_lpmode, port):
    lpmode = sfp_lpmode.get(port)
    if lpmode is None:
        logging.error(f"Interface {port} does not present in the show command")
        return False

    if lpmode not in ["Off", "On"]:
        logging.error("Invalid low-power mode {} for port {}".format(lpmode, port))
        return False

    return True
