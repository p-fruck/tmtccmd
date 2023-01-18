"""
@brief      Core components for mode commanding (custom PUS service)
"""
import enum
import struct
import deprecation
from typing import Union

from spacepackets.ecss import PusTelecommand
from tmtccmd import __version__
from tmtccmd.pus import CustomPusService
from tmtccmd.pus.s200_fsfw_mode import Subservice


class Mode(enum.IntEnum):
    OFF = 0
    ON = 1
    NORMAL = 2
    RAW = 3


def pack_mode_data(object_id: bytes, mode: Union[Mode, int], submode: int) -> bytearray:
    """FSFW modes: Mode 0: Off, Mode 1: Mode On, Mode 2: Mode Normal, Mode 3: Mode Raw"""
    mode_data = bytearray()
    mode_data += object_id + struct.pack("!I", mode) + struct.pack("B", submode)
    return mode_data


@deprecation.deprecated(
    deprecated_in="v4.0.0a1",
    current_version=__version__,
    details="use create_... API instead",
)
def pack_mode_command(
    object_id: bytes, mode: Union[int, Mode], submode: int
) -> PusTelecommand:
    return create_mode_command(object_id, mode, submode)


def create_mode_command(
    object_id: bytes, mode: Union[int, Mode], submode: int
) -> PusTelecommand:
    return PusTelecommand(
        service=CustomPusService.SERVICE_200_MODE,
        subservice=Subservice.TC_MODE_COMMAND,
        app_data=pack_mode_data(object_id, mode, submode),
    )
