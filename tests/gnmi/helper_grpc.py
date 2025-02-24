import logging
import grpc

from tests.gnmi.protos.gnoi.system import system_pb2_grpc
from tests.common.helpers.gnmi_utils import GNMIEnvironment



def create_grpc_stub(duthost):
    """
    Helper function to create a gRPC stub for the gNOI System service
    """
    # Get DUT gRPC server address and port
    ip = duthost.mgmt_ip
    env = GNMIEnvironment(duthost, GNMIEnvironment.GNMI_MODE)
    port = env.gnmi_port
    target = f"{ip}:{port}"

    # Load the TLS certificates
    with open("gnmiCA.pem", "rb") as f:
        root_certificates = f.read()
    with open("gnmiclient.crt", "rb") as f:
        client_certificate = f.read()
    with open("gnmiclient.key", "rb") as f:
        client_key = f.read()

    # Create SSL credentials
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_certificates,
        private_key=client_key,
        certificate_chain=client_certificate
    )

    # Create gRPC channel
    logging.info("Creating gRPC secure channel to %s", target)

    channel = grpc.secure_channel(target, credentials)
    try:
        grpc.channel_ready_future(channel).result(timeout=10)
        logging.info("gRPC channel is ready")
    except grpc.FutureTimeoutError as e:
        logging.error("Error: gRPC channel not ready: %s", e)
        pytest.fail("Failed to connect to gRPC server")

    # Create gRPC stub
    stub = system_pb2_grpc.SystemStub(channel)
    return stub


