from dataclasses import dataclass
from typing import Callable
from spacepackets.cfdp.defs import (
    FaultHandlerCodes,
    ChecksumTypes,
    UnsignedByteField,
    LenInBytes,
)

# User can specify a function which takes the fault handler code as an argument and returns nothing
FaultHandlerT = Callable[[FaultHandlerCodes], None]


@dataclass
class LocalIndicationCfg:
    eof_sent_indication_required: bool
    eof_recv_indication_required: bool
    file_segment_recvd_indication_required: bool
    transaction_finished_indication_required: bool
    suspended_indication_required: bool
    resumed_indication_required: bool


@dataclass
class LocalEntityCfg:
    local_entity_id: UnsignedByteField
    indication_cfg: LocalIndicationCfg
    default_fault_handlers: FaultHandlerT


@dataclass
class RemoteEntityCfg:
    remote_entity_id: UnsignedByteField
    max_file_segment_len: int
    crc_on_transmission: bool
    # TODO: Hardcoded for now
    crc_type: ChecksumTypes = ChecksumTypes.CRC_32


class RemoteEntityTable:
    def __init__(self):
        self._remote_entity_dict = dict()

    def add_remote_entity(self, cfg: RemoteEntityCfg) -> bool:
        if cfg.remote_entity_id in self._remote_entity_dict:
            return False
        self._remote_entity_dict.update({cfg.remote_entity_id: cfg})
        return True

    def get_remote_entity(self, remote_entity_id: bytes) -> RemoteEntityCfg:
        return self._remote_entity_dict.get(remote_entity_id)
