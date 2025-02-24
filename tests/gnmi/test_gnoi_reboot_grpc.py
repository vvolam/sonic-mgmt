import pytest
import logging
import grpc

from tests.gnmi.protos.gnoi.system import system_pb2_grpc, system_pb2
from tests.common.helpers.gnmi_utils import GNMIEnvironment
from tests.gnmi.helper_grpc import create_grpc_stub
from tests.common.reboot import wait_for_startup
from tests.common.platform.processes_utils import wait_critical_processes, check_critical_processes

pytestmark = [
    pytest.mark.topology('any')
]


"""
This module contains tests for the gNOI System Services for reboot, using gRPC python API.
"""

def _get_gnoi_stubs():
    PROTO_ROOT = "gnmi/protos"
    sys.path.append(os.path.abspath(PROTO_ROOT))
    from gnoi.system import system_pb2_grpc, system_pb2
    return system_pb2_grpc, system_pb2


def test_gnoi_system_reboot_invalid_method(duthosts, rand_one_dut_hostname):
    """
    Verify the gNOI System Reboot API with an invalid method
    """
    duthost = duthosts[rand_one_dut_hostname]
    system_pb2_grpc, system_pb2 = _get_gnoi_stubs()
    stub = create_grpc_stub(duthost)

    # Use an invalid reboot method
    request = system_pb2.RebootRequest(method=999, message="Test invalid reboot method")
    try:
        response = stub.Reboot(request)
        logging.info("Received response: %s", response)
        pytest.fail("Expected an exception for invalid method, but got a response")
    except grpc.RpcError as e:
        logging.info("Received expected gRPC error: %s", e)
        assert e.code() == grpc.StatusCode.INVALID_ARGUMENT, \
            f"Expected INVALID_ARGUMENT status, but got {e.code()}"


def test_gnoi_system_reboot_missing_certificates(duthosts, rand_one_dut_hostname):
    """
    Verify the gNOI System Reboot API with missing certificates
    """
    duthost = duthosts[rand_one_dut_hostname]
    system_pb2_grpc, system_pb2 = _get_gnoi_stubs()

    # Get DUT gRPC server address and port
    ip = duthost.mgmt_ip
    env = GNMIEnvironment(duthost, GNMIEnvironment.GNMI_MODE)
    port = env.gnmi_port
    target = f"{ip}:{port}"

    # Create SSL credentials with missing certificates
    credentials = grpc.ssl_channel_credentials()

    # Create gRPC channel
    logging.info("Creating gRPC secure channel to %s", target)

    with grpc.secure_channel(target, credentials) as channel:
        try:
            grpc.channel_ready_future(channel).result(timeout=10)
            logging.info("gRPC channel is ready")
        except grpc.FutureTimeoutError as e:
            logging.error("Error: gRPC channel not ready: %s", e)
            pytest.fail("Failed to connect to gRPC server")

        # Create gRPC stub
        stub = system_pb2_grpc.SystemStub(channel)

        # Attempt to reboot the device
        request = system_pb2.RebootRequest(method=system_pb2.RebootMethod.HALT, message="Test missing certificates")
        try:
            response = stub.Reboot(request)
            logging.info("Received response: %s", response)
            pytest.fail("Expected an exception for missing certificates, but got a response")
        except grpc.RpcError as e:
            logging.info("Received expected gRPC error: %s", e)
            assert e.code() == grpc.StatusCode.UNAVAILABLE, \
                f"Expected UNAVAILABLE status, but got {e.code()}"


def test_gnoi_system_reboot_halt_method(duthosts, rand_one_dut_hostname, localhost):
    """
    Verify the gNOI System Reboot API with an invalid method
    """
    duthost = duthosts[rand_one_dut_hostname]
    system_pb2_grpc, system_pb2 = _get_gnoi_stubs()

    # Check if "sudo reboot -p" command is present on duthost
    if not duthost.command("sudo reboot -h | grep -i pre-shutdown", module_ignore_errors=True)['rc'] == 0:
        pytest.skip("Skipping test because 'sudo reboot -p' command is not present on duthost")

    stub = create_grpc_stub(duthost)

    # Use an HALT reboot method
    request = system_pb2.RebootRequest(method=system_pb2.RebootMethod.HALT, message="Test HALT method")
    response = stub.Reboot(request)
    if response.code != 0:
        logging.info("Received non-zero response code: %s", response.code)
        pytest.fail(f"Expected zero response code, but got {response.code}")
    else:
        logging.info("Received response: %s", response)

    wait_critical_processes(duthost)
    check_critical_processes(duthost, watch_secs=10)


def test_gnoi_system_reboot_cold_method(duthosts, rand_one_dut_hostname, localhost):
    """
    Verify the gNOI System Reboot API with an invalid method
    """
    duthost = duthosts[rand_one_dut_hostname]
    system_pb2_grpc, system_pb2 = _get_gnoi_stubs()

    stub = create_grpc_stub(duthost)

    # Use an COLD reboot method
    request = system_pb2.RebootRequest(method=system_pb2.RebootMethod.COLD, message="Test COLD method")
    response = stub.Reboot(request)
    if response.code != 0:
        logging.info("Received non-zero response code: %s", response.code)
        pytest.fail(f"Expected zero response code, but got {response.code}")
    else:
        logging.info("Received response: %s", response)

    wait_for_startup(duthost, localhost, delay=10, timeout=300)
    wait_critical_processes(duthost)
    if duthost.facts['hwsku'] in {"Nokia-M0-7215", "Nokia-7215"}:
        wait_critical_processes(duthost)

