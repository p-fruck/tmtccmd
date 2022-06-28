"""Contains the TmListener which can be used to listen to Telemetry in the background"""
from typing import Dict, List, Tuple

from spacepackets.ccsds.spacepacket import get_apid_from_raw_space_packet

from tmtccmd.tm import TelemetryQueueT, CcsdsTmHandler
from tmtccmd.logging import get_console_logger
from tmtccmd.com_if import ComInterface

LOGGER = get_console_logger()

INVALID_APID = -2
UNKNOWN_TARGET_ID = -1
QueueDictT = Dict[int, Tuple[TelemetryQueueT, int]]
QueueListT = List[Tuple[int, TelemetryQueueT]]


class CcsdsTmListener:
    """Performs all TM listening operations.
    This listener to have a permanent means to receive data. A background thread is used
    to poll data with the provided communication interface. Dedicated sender and receiver object
    or any other software component can get the received packets from the internal deque container.
    """

    def __init__(
        self,
        tm_handler: CcsdsTmHandler,
    ):
        """Initiate a TM listener.
        :param tm_handler: If valid CCSDS packets are found, they are dispatched to
            the passed handler
        """
        self.__tm_handler = tm_handler

    def operation(self, com_if: ComInterface) -> int:
        packet_list = com_if.receive()
        for tm_packet in packet_list:
            self.__handle_ccsds_space_packet(tm_packet)
        return len(packet_list)

    def __handle_ccsds_space_packet(self, tm_packet: bytes):
        if len(tm_packet) < 6:
            LOGGER.warning("TM packet to small to be a CCSDS space packet")
        else:
            apid = get_apid_from_raw_space_packet(tm_packet)
            self.__tm_handler.handle_packet(apid, tm_packet)
            return True
        return False
