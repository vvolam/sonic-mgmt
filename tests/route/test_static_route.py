import pytest
import json
import ipaddress
import time
import logging
import natsort
import random
import six
from collections import defaultdict

from tests.common.fixtures.ptfhost_utils import change_mac_addresses, copy_arp_responder_py # noqa F811
from tests.common.fixtures.ptfhost_utils import remove_ip_addresses # noqa F811
from tests.common.dualtor.dual_tor_utils import mux_cable_server_ip
from tests.common.dualtor.mux_simulator_control import mux_server_url # noqa F811
from tests.common.dualtor.dual_tor_utils import show_muxcable_status
from tests.common.dualtor.mux_simulator_control import toggle_all_simulator_ports_to_rand_selected_tor_m # noqa F811
from tests.common.utilities import wait_until, get_intf_by_sub_intf
from tests.common.utilities import get_neighbor_ptf_port_list
from tests.common.helpers.assertions import pytest_assert
from tests.common.helpers.assertions import pytest_require
from tests.common.helpers.constants import UPSTREAM_NEIGHBOR_MAP
from tests.common import config_reload
import ptf.testutils as testutils
import ptf.mask as mask
import ptf.packet as packet
from tests.common import constants
from tests.common.flow_counter.flow_counter_utils import RouteFlowCounterTestContext, is_route_flow_counter_supported # noqa F811
from tests.common.helpers.dut_ports import get_vlan_interface_list, get_vlan_interface_info


pytestmark = [
    pytest.mark.topology('t0', 'm0', 'mx'),
    pytest.mark.device_type('vs')
]


def is_dualtor(tbinfo):
    """Check if the testbed is dualtor."""
    return "dualtor" in tbinfo["topo"]["name"]


def add_ipaddr(ptfadapter, ptfhost, nexthop_addrs, prefix_len, nexthop_interfaces, ipv6=False):
    if ipv6:
        for idx in range(len(nexthop_addrs)):
            ptfhost.shell("ip -6 addr add {}/{} dev eth{}".format(
                nexthop_addrs[idx], prefix_len, nexthop_interfaces[idx]), module_ignore_errors=True
            )
    else:
        vlan_host_map = defaultdict(dict)
        for idx in range(len(nexthop_addrs)):
            mac = ptfadapter.dataplane.get_mac(
                0, int(get_intf_by_sub_intf(nexthop_interfaces[idx]))
            ).decode().replace(":", "")
            vlan_host_map[nexthop_interfaces[idx]][nexthop_addrs[idx]] = mac

        arp_responder_conf = {}
        for port in vlan_host_map:
            arp_responder_conf['eth{}'.format(port)] = vlan_host_map[port]

        with open("/tmp/from_t1.json", "w") as ar_config:
            json.dump(arp_responder_conf, ar_config)
        ptfhost.copy(src="/tmp/from_t1.json", dest="/tmp/from_t1.json")
        ptfhost.host.options["variable_manager"].extra_vars.update({"arp_responder_args": "-e"})
        ptfhost.template(src="templates/arp_responder.conf.j2", dest="/etc/supervisor/conf.d/arp_responder.conf")

        ptfhost.shell('supervisorctl reread && supervisorctl update')
        ptfhost.shell('supervisorctl restart arp_responder')


def del_ipaddr(ptfhost, nexthop_addrs, prefix_len, nexthop_devs, ipv6=False):
    if ipv6:
        for idx in range(len(nexthop_addrs)):
            ptfhost.shell(
                "ip -6 addr del {}/{} dev eth{}".format(
                    nexthop_addrs[idx], prefix_len, nexthop_devs[idx]
                ),
                module_ignore_errors=True
            )
    else:
        ptfhost.shell('supervisorctl stop arp_responder', module_ignore_errors=True)


def clear_arp_ndp(duthost, ipv6=False):
    if ipv6:
        duthost.shell("sonic-clear ndp")
    else:
        duthost.shell("sonic-clear arp")


def generate_and_verify_traffic(duthost, ptfadapter, tbinfo, ip_dst, expected_ports, ipv6=False):
    if ipv6:
        pkt = testutils.simple_tcpv6_packet(
            eth_dst=duthost.facts["router_mac"],
            eth_src=ptfadapter.dataplane.get_mac(*list(ptfadapter.dataplane.ports.keys())[0]),
            ipv6_src='2001:db8:85a3::8a2e:370:7334',
            ipv6_dst=ip_dst,
            ipv6_hlim=64,
            tcp_sport=1234,
            tcp_dport=4321)
    else:
        pkt = testutils.simple_tcp_packet(
            eth_dst=duthost.facts["router_mac"],
            eth_src=ptfadapter.dataplane.get_mac(*list(ptfadapter.dataplane.ports.keys())[0]),
            ip_src='1.1.1.1',
            ip_dst=ip_dst,
            ip_ttl=64,
            tcp_sport=1234,
            tcp_dport=4321)

    exp_pkt = pkt.copy()
    exp_pkt = mask.Mask(exp_pkt)
    exp_pkt.set_do_not_care_scapy(packet.Ether, 'dst')
    exp_pkt.set_do_not_care_scapy(packet.Ether, 'src')
    if ipv6:
        exp_pkt.set_do_not_care_scapy(packet.IPv6, 'hlim')
    else:
        exp_pkt.set_do_not_care_scapy(packet.IP, 'ttl')
        exp_pkt.set_do_not_care_scapy(packet.IP, 'chksum')

    topo_type = tbinfo["topo"]["type"]
    pytest_require(topo_type in UPSTREAM_NEIGHBOR_MAP, "Unsupported topo: {}".format(topo_type))
    upstream_name = UPSTREAM_NEIGHBOR_MAP[topo_type]
    ptf_upstream_intf = random.choice(get_neighbor_ptf_port_list(duthost, upstream_name, tbinfo))
    ptfadapter.dataplane.flush()
    testutils.send(ptfadapter, ptf_upstream_intf, pkt)
    testutils.verify_packet_any_port(ptfadapter, exp_pkt, ports=expected_ports)


def wait_all_bgp_up(duthost):
    config_facts = duthost.config_facts(host=duthost.hostname, source="running")['ansible_facts']
    bgp_neighbors = config_facts.get('BGP_NEIGHBOR', {})
    if not wait_until(300, 10, 0, duthost.check_bgp_session_state, list(bgp_neighbors.keys())):
        pytest.fail("not all bgp sessions are up after config reload")


def check_route_redistribution(duthost, prefix, ipv6, removed=False):
    if ipv6:
        SHOW_BGP_SUMMARY_CMD = "show ipv6 bgp summary"
        SHOW_BGP_ADV_ROUTES_CMD_TEMPLATE = "show ipv6 bgp neighbor {} advertised-routes"
    else:
        SHOW_BGP_SUMMARY_CMD = "show ip bgp summary"
        SHOW_BGP_ADV_ROUTES_CMD_TEMPLATE = "show ip bgp neighbor {} advertised-routes"

    bgp_summary = duthost.show_and_parse(SHOW_BGP_SUMMARY_CMD)

    # Collect neighbors, excluding those with 'PT0' in the neighbor name
    bgp_neighbors = [
        entry["neighbhor"]
        for entry in bgp_summary
        if "PT0" not in entry.get("neighborname", "")
    ]

    if not bgp_neighbors:
        pytest.fail("No valid BGP neighbors found (excluding PT0).")

    def _check_routes():
        for neighbor in bgp_neighbors:
            adv_routes = duthost.shell(SHOW_BGP_ADV_ROUTES_CMD_TEMPLATE.format(neighbor))["stdout"]
            if removed and prefix in adv_routes:
                logging.info(f"Route {prefix} is still advertised by {neighbor} (expected removed).")
                return False
            if not removed and prefix not in adv_routes:
                logging.info(f"Route {prefix} is NOT advertised by {neighbor} (expected present).")
                return False
        return True

    pytest_assert(
        wait_until(60, 15, 0, _check_routes),
        f"Route {prefix} advertisement state does not match expected 'removed={removed}' on all neighbors"
    )


# output example of ip [-6] route show
# ip route show 1.1.1.0/24
# 1.1.1.0/24 proto 196 metric 20
#        nexthop via 192.168.0.2 dev Vlan1000 weight 1
#        nexthop via 192.168.0.3 dev Vlan1000 weight 1
#        nexthop via 192.168.0.4 dev Vlan1000 weight 1
# ip -6 route show 20c0:afa8::/64
# 20c0:afa8::/64 proto bgp src fc00:1::32 metric 20
#        nexthop via fc00::22 dev PortChannel101 weight 1
#        nexthop via fc00::26 dev PortChannel102 weight 1
#        nexthop via fc00::2a dev PortChannel103 weight 1
#        nexthop via fc00::2e dev PortChannel104 weight 1 pref medium
def check_static_route(duthost, prefix, nexthop_addrs, ipv6):
    if ipv6:
        SHOW_STATIC_ROUTE_CMD = "ip -6 route show {}".format(prefix)
    else:
        SHOW_STATIC_ROUTE_CMD = "ip route show {}".format(prefix)
    output = duthost.shell(SHOW_STATIC_ROUTE_CMD, module_ignore_errors=True)["stdout"].split("\n")

    def _check_nh_in_output(nexthop):
        for line in output:
            if nexthop in line:
                return True
        return False

    check_result = True
    for nh in nexthop_addrs:
        if not _check_nh_in_output(nh):
            check_result = False

    assert check_result, "config static route: {} nexthop {}\nreal:\n{}".format(
        prefix, ",".join(nexthop_addrs), output
    )


def check_mux_status(duthost, expected_status):
    show_mux_status_ret = show_muxcable_status(duthost)
    status_values = set([intf_status['status'] for intf_status in show_mux_status_ret.values()])
    return status_values == {expected_status}


def run_static_route_test(duthost, unselected_duthost, ptfadapter, ptfhost, tbinfo,
                          prefix, nexthop_addrs, prefix_len, nexthop_devs, nexthop_interfaces,
                          is_route_flow_counter_supported, ipv6=False, config_reload_test=False): # noqa F811
    is_dual_tor = False
    if 'dualtor' in tbinfo['topo']['name'] and unselected_duthost is not None:
        is_dual_tor = True

    # Clean up arp or ndp
    clear_arp_ndp(duthost, ipv6=ipv6)
    if is_dual_tor:
        clear_arp_ndp(unselected_duthost, ipv6=ipv6)

    # Add ipaddresses in ptf
    add_ipaddr(ptfadapter, ptfhost, nexthop_addrs, prefix_len, nexthop_interfaces, ipv6=ipv6)

    try:
        # Add static route
        duthost.shell("sonic-db-cli CONFIG_DB hmset 'STATIC_ROUTE|{}' nexthop {}".format(
                prefix, ",".join(nexthop_addrs)
            )
        )
        if is_dual_tor:
            unselected_duthost.shell(
                "sonic-db-cli CONFIG_DB hmset 'STATIC_ROUTE|{}' nexthop {}".format(
                    prefix, ",".join(nexthop_addrs)
                )
            )

        time.sleep(5)

        # check if the static route in kernel is what we expect
        check_static_route(duthost, prefix, nexthop_addrs, ipv6=ipv6)

        # Check traffic get forwarded to the nexthop
        ip_dst = str(ipaddress.ip_network(six.text_type(prefix))[1])
        # try to refresh arp entry before traffic testing to improve stability
        for nexthop_addr in nexthop_addrs:
            duthost.shell("timeout 1 ping -c 1 -w 1 {}".format(nexthop_addr), module_ignore_errors=True)
        with RouteFlowCounterTestContext(is_route_flow_counter_supported,
                                         duthost, [prefix], {prefix: {'packets': '1'}}):
            generate_and_verify_traffic(duthost, ptfadapter, tbinfo, ip_dst, nexthop_devs, ipv6=ipv6)

        # Check the route is advertised to the neighbors
        check_route_redistribution(duthost, prefix, ipv6)

        # Config save and reload if specified
        if config_reload_test:
            # config reload on active tor
            duthost.shell('config save -y')
            if duthost.facts["platform"] == "x86_64-cel_e1031-r0":
                config_reload(duthost, wait=500)
            else:
                config_reload(duthost, wait=450)
            # On dualtor, config_reload can result in a switchover (active tor can become standby and viceversa).
            # So we need to make sure rand_selected_dut is in active state before verifying traffic.
            if is_dual_tor:
                duthost.shell("config mux mode active all")
                unselected_duthost.shell("config mux mode standby all")
                pytest_assert(wait_until(60, 5, 0, check_mux_status, duthost, 'active'),
                              "Could not config ports to active on {}".format(duthost.hostname))
                pytest_assert(wait_until(60, 5, 0, check_mux_status, unselected_duthost, 'standby'),
                              "Could not config ports to standby on {}".format(unselected_duthost.hostname))
            # FIXME: We saw re-establishing BGP sessions can takes around 7 minutes
            # on some devices (like 4600) after config reload, so we need below patch
            wait_all_bgp_up(duthost)
            for nexthop_addr in nexthop_addrs:
                duthost.shell("timeout 1 ping -c 1 -w 1 {}".format(nexthop_addr), module_ignore_errors=True)
            with RouteFlowCounterTestContext(is_route_flow_counter_supported, duthost,
                                             [prefix], {prefix: {'packets': '1'}}):
                generate_and_verify_traffic(duthost, ptfadapter, tbinfo, ip_dst, nexthop_devs, ipv6=ipv6)
            check_route_redistribution(duthost, prefix, ipv6)

    finally:
        # Remove static route
        duthost.shell("sonic-db-cli CONFIG_DB del 'STATIC_ROUTE|{}'".format(prefix), module_ignore_errors=True)
        if is_dual_tor:
            unselected_duthost.shell("sonic-db-cli CONFIG_DB del 'STATIC_ROUTE|{}'".format(prefix),
                                     module_ignore_errors=True)

        # Delete ipaddresses in ptf
        del_ipaddr(ptfhost, nexthop_addrs, prefix_len, nexthop_devs, ipv6=ipv6)

        # Check the advertised route get removed
        time.sleep(5)
        check_route_redistribution(duthost, prefix, ipv6, removed=True)

        # Config save if the saved config_db was updated
        if config_reload_test:
            duthost.shell('config save -y')
            if is_dual_tor:
                unselected_duthost.shell('config save -y')

        # Clean up arp or ndp
        clear_arp_ndp(duthost, ipv6=ipv6)
        if is_dual_tor:
            clear_arp_ndp(unselected_duthost, ipv6=ipv6)


def get_nexthops(duthost, tbinfo, ipv6=False, count=1):
    mg_facts = duthost.get_extended_minigraph_facts(tbinfo)

    # Filter VLANs with one interface inside only(PortChannel interface in case of t0-56-po2vlan topo)
    unexpected_vlans = []
    for vlan, vlan_data in list(mg_facts['minigraph_vlans'].items()):
        if len(vlan_data['members']) < 2:
            unexpected_vlans.append(vlan)

    # Update minigraph_vlan_interfaces with only expected VLAN interfaces
    expected_vlan_ifaces = []
    for vlan in unexpected_vlans:
        for mg_vl_iface in mg_facts['minigraph_vlan_interfaces']:
            if vlan != mg_vl_iface['attachto']:
                expected_vlan_ifaces.append(mg_vl_iface)
    if expected_vlan_ifaces:
        mg_facts['minigraph_vlan_interfaces'] = expected_vlan_ifaces

    vlan_interfaces = get_vlan_interface_list(duthost)
    # pick up the first vlan to test
    vlan_if_name = vlan_interfaces[0]
    if ipv6:
        vlan_intf = get_vlan_interface_info(duthost, tbinfo, vlan_if_name, "ipv6")
    else:
        vlan_intf = get_vlan_interface_info(duthost, tbinfo, vlan_if_name, "ipv4")
    prefix_len = vlan_intf['prefixlen']

    is_backend_topology = mg_facts.get(constants.IS_BACKEND_TOPOLOGY_KEY, False)
    if is_dualtor(tbinfo):
        server_ips = mux_cable_server_ip(duthost)
        vlan_intfs = natsort.natsorted(list(server_ips.keys()))
        nexthop_devs = [mg_facts["minigraph_ptf_indices"][_] for _ in vlan_intfs]
        server_ip_key = "server_ipv6" if ipv6 else "server_ipv4"
        nexthop_addrs = [server_ips[_][server_ip_key].split("/")[0] for _ in vlan_intfs]
        nexthop_interfaces = nexthop_devs
    else:
        vlan_subnet = ipaddress.ip_network(vlan_intf['subnet'])
        vlan = mg_facts['minigraph_vlans'][vlan_if_name]
        vlan_ports = vlan['members']
        vlan_id = vlan['vlanid']
        vlan_ptf_ports = [mg_facts['minigraph_ptf_indices'][port] for port in vlan_ports if 'PortChannel' not in port]
        nexthop_devs = vlan_ptf_ports
        # backend topology use ethx.x(e.g. eth30.1000) during servers and T0 in ptf
        # in other topology use ethx(e.g. eth30)
        if is_backend_topology:
            nexthop_interfaces = [str(dev) + constants.VLAN_SUB_INTERFACE_SEPARATOR +
                                  str(vlan_id) for dev in nexthop_devs]
        else:
            nexthop_interfaces = nexthop_devs
        nexthop_addrs = [str(vlan_subnet[i + 2]) for i in range(len(nexthop_devs))]
    count = min(count, len(nexthop_devs))
    indices = random.sample(list(range(len(nexthop_devs))), k=count)
    return (
        prefix_len,
        [nexthop_addrs[_] for _ in indices],
        [nexthop_devs[_] for _ in indices],
        [nexthop_interfaces[_] for _ in indices],
    )


def test_static_route(rand_selected_dut, rand_unselected_dut, ptfadapter, ptfhost, tbinfo,
                      setup_standby_ports_on_rand_unselected_tor, # noqa F811
                      toggle_all_simulator_ports_to_rand_selected_tor_m, is_route_flow_counter_supported): # noqa F811
    duthost = rand_selected_dut
    unselected_duthost = rand_unselected_dut
    prefix_len, nexthop_addrs, nexthop_devs, nexthop_interfaces = get_nexthops(duthost, tbinfo)
    run_static_route_test(duthost, unselected_duthost, ptfadapter, ptfhost, tbinfo, "1.1.1.0/24",
                          nexthop_addrs, prefix_len, nexthop_devs, nexthop_interfaces, is_route_flow_counter_supported)


@pytest.mark.disable_loganalyzer
def test_static_route_ecmp(rand_selected_dut, rand_unselected_dut, ptfadapter, ptfhost, tbinfo,
                           setup_standby_ports_on_rand_unselected_tor, # noqa F811
                           toggle_all_simulator_ports_to_rand_selected_tor_m, is_route_flow_counter_supported): # noqa F811
    duthost = rand_selected_dut
    unselected_duthost = rand_unselected_dut
    prefix_len, nexthop_addrs, nexthop_devs, nexthop_interfaces = get_nexthops(duthost, tbinfo, count=3)
    run_static_route_test(duthost, unselected_duthost, ptfadapter, ptfhost, tbinfo, "2.2.2.0/24",
                          nexthop_addrs, prefix_len, nexthop_devs, nexthop_interfaces,
                          is_route_flow_counter_supported, config_reload_test=True)


def test_static_route_ipv6(rand_selected_dut, rand_unselected_dut, ptfadapter, ptfhost, tbinfo,
                           setup_standby_ports_on_rand_unselected_tor, # noqa F811
                           toggle_all_simulator_ports_to_rand_selected_tor_m, is_route_flow_counter_supported): # noqa F811
    duthost = rand_selected_dut
    unselected_duthost = rand_unselected_dut
    prefix_len, nexthop_addrs, nexthop_devs, nexthop_interfaces = get_nexthops(duthost, tbinfo, ipv6=True)
    run_static_route_test(duthost, unselected_duthost, ptfadapter, ptfhost, tbinfo, "2000:1::/64",
                          nexthop_addrs, prefix_len, nexthop_devs, nexthop_interfaces,
                          is_route_flow_counter_supported, ipv6=True)


@pytest.mark.disable_loganalyzer
def test_static_route_ecmp_ipv6(rand_selected_dut, rand_unselected_dut, ptfadapter, ptfhost, tbinfo,
                                setup_standby_ports_on_rand_unselected_tor, # noqa F811
                                toggle_all_simulator_ports_to_rand_selected_tor_m, is_route_flow_counter_supported): # noqa F811
    duthost = rand_selected_dut
    unselected_duthost = rand_unselected_dut
    prefix_len, nexthop_addrs, nexthop_devs, nexthop_interfaces = get_nexthops(duthost, tbinfo, ipv6=True, count=3)
    run_static_route_test(duthost, unselected_duthost, ptfadapter, ptfhost, tbinfo, "2000:2::/64",
                          nexthop_addrs, prefix_len, nexthop_devs, nexthop_interfaces,
                          is_route_flow_counter_supported, ipv6=True, config_reload_test=True)
