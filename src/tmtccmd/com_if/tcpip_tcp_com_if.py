"""
:file:      tcpip_tcp_com_if.py
:date:      13.05.2021
:brief:     TCP communication interface
:author:    R. Mueller
"""
import select
import socket
import time
import threading
from collections import deque
from typing import Union

from tmtccmd.utility.logger import get_logger
from tmtccmd.config.definitions import CoreModeList
from tmtccmd.com_if.com_interface_base import CommunicationInterface, PusTmListT
from tmtccmd.pus_tm.factory import PusTelemetryFactory
from tmtccmd.utility.tmtc_printer import TmTcPrinter
from tmtccmd.ecss.tc import PusTelecommand
from tmtccmd.config.definitions import EthernetAddressT

LOGGER = get_logger()

TCP_RECV_WIRETAPPING_ENABLED = False
TCP_SEND_WIRETAPPING_ENABLED = False


# pylint: disable=abstract-method
# pylint: disable=arguments-differ
# pylint: disable=too-many-arguments
class TcpIpTcpComIF(CommunicationInterface):
    """
    Communication interface for UDP communication.
    """
    def __init__(
            self, com_if_key: str, tm_polling_freqency: int, tm_timeout: float, tc_timeout_factor: float,
            send_address: EthernetAddressT, max_recv_size: int, tmtc_printer: Union[None, TmTcPrinter] = None,
            init_mode: int = CoreModeList.LISTENER_MODE):
        """
        Initialize a communication interface to send and receive UDP datagrams.
        :param tm_timeout:
        :param tc_timeout_factor:
        :param send_address:
        :param max_recv_size:
        :param recv_addr:
        :param tmtc_printer: Printer instance, can be passed optionally to allow packet debugging
        """
        super().__init__(com_if_key=com_if_key, tmtc_printer=tmtc_printer)
        self.tm_timeout = tm_timeout
        self.tc_timeout_factor = tc_timeout_factor
        self.tm_polling_frequency = tm_polling_freqency
        self.send_address = send_address
        self.max_recv_size = max_recv_size
        self.init_mode = init_mode

        self.__tcp_socket = None
        self.__last_connection_time = 0
        self.__tm_thread_kill_signal = threading.Event()
        self.__tcp_conn_thread = threading.Thread(target=self.__tcp_tm_client, daemon=True)
        self.__tm_queue = deque()
        self.__socket_lock = threading.Lock()

    def __del__(self):
        try:
            self.close()
        except IOError:
            LOGGER.warning("Could not close UDP communication interface!")

    def initialize(self, args: any = None) -> any:
        self.__tm_thread_kill_signal.clear()

    def open(self, args: any = None):
        self.__tcp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.__tcp_conn_thread.start()

    def close(self, args: any = None) -> None:
        self.__tm_thread_kill_signal.set()
        self.__tcp_conn_thread.join(self.tm_polling_frequency)
        if self.__tcp_socket is not None:
            self.__tcp_socket.close()

    def send_data(self, data: bytearray):
        with self.__socket_lock:
            self.__tcp_socket.connect(self.send_address)
            self.__tcp_socket.sendto(data, self.send_address)
            self.__tcp_socket.shutdown(SHUT_WR)
            self.__receive_tm_packets()
            self.__last_connection_time = time.time()
            self.__tcp_socket.close()

    def send_telecommand(self, tc_packet: bytearray, tc_packet_obj: PusTelecommand) -> None:
        if self.__tcp_socket is None:
            return
        bytes_sent = self.__tcp_socket.sendto(tc_packet, self.send_address)
        if bytes_sent != len(tc_packet):
            LOGGER.warning("Not all bytes were sent!")

    def receive_telemetry(self, poll_timeout: float = 0) -> PusTmListT:
        tm_packet_list = []
        if self.__tcp_socket is None:
            return tm_packet_list
        while self.__tm_queue:
            tm_packet_list.append(self.__tm_queue.pop())
        return tm_packet_list

    def __tcp_tm_client(self):
        while True and not self.__tm_thread_kill_signal.is_set():
            if time.time() - self.__last_connection_time >= self.tm_polling_frequency:
                with self.__socket_lock:
                    self.__tcp_socket.connect(self.send_address)
                    self.__tcp_socket.shutdown(SHUT_WR)
                    self.__receive_tm_packets()
                    self.__last_connection_time = time.time()
            time.sleep(self.tm_polling_frequency / 2.0)

    def __receive_tm_packets(self):
        while True:
            bytes_recvd = self.__tcp_socket.recv(self.max_recv_size)
            if bytes_recvd > 0:
                tm_packet = PusTelemetryFactory.create(bytearray(data))
                self.__tm_queue.appendleft(tm_packet)
            elif bytes_recvd == 0:
                break
