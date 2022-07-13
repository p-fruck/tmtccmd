from __future__ import annotations
import enum
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Deque, cast, Optional

from spacepackets.cfdp import (
    PduType,
    ChecksumTypes,
    TransmissionModes,
    ConditionCode,
    TlvTypes,
)
from spacepackets.cfdp.pdu import (
    DirectiveType,
    AbstractFileDirectiveBase,
    MetadataPdu,
    FileDataPdu,
    EofPdu,
)
from spacepackets.cfdp.pdu.helper import GenericPduPacket, PduHolder
from tmtccmd.cfdp import RemoteEntityCfg, CfdpUserBase, LocalEntityCfg
from tmtccmd.cfdp.defs import CfdpStates, TransactionId
from tmtccmd.cfdp.handler.defs import FileParamsBase
from tmtccmd.cfdp.user import MetadataRecvParams, FileSegmentRecvParams


@dataclass
class DestFileParams(FileParamsBase):
    file_name: Path

    @classmethod
    def empty(cls) -> DestFileParams:
        return cls(offset=0, segment_len=0, crc32=bytes(), size=0, file_name=Path())

    def reset(self):
        super().reset()
        self.file_name = Path()


class TransactionStep(enum.Enum):
    IDLE = 0
    # Metadata was received
    TRANSACTION_START = 1
    RECEIVING_FILE_DATA = 2
    SENDING_ACK_PDU = 3
    # File transfer complete. Perform checksum verification and notice of completion
    TRANSFER_COMPLETION = 4
    SENDING_FINISHED_PDU = 5


@dataclass
class DestStateWrapper:
    state: CfdpStates = CfdpStates.IDLE
    transaction: TransactionStep = TransactionStep.IDLE
    packet_ready: bool = False


class FsmResult:
    def __init__(self, states: DestStateWrapper, pdu_holder: PduHolder):
        self.states = states
        self.pdu_holder = pdu_holder


class DestHandler:
    def __init__(self, cfg: LocalEntityCfg, user: CfdpUserBase):
        self.cfg = cfg
        self.states = DestStateWrapper()
        self.user = user
        self._pdu_holder = PduHolder(None)
        self._transaction_id: Optional[TransactionId] = None
        self._checksum_type = ChecksumTypes.NULL_CHECKSUM
        self._closure_requested = False
        self._fp = DestFileParams.empty()
        self._file_directives_dict: Dict[
            DirectiveType, List[AbstractFileDirectiveBase]
        ] = dict()
        self._file_data_deque: Deque[FileDataPdu] = deque()

    def _start_transaction(self, metadata_pdu: MetadataPdu) -> bool:
        if self.states.state != CfdpStates.IDLE:
            return False
        self.states.transaction = TransactionStep.TRANSACTION_START
        if metadata_pdu.pdu_header.trans_mode == TransmissionModes.UNACKNOWLEDGED:
            self.states.state = CfdpStates.BUSY_CLASS_1_NACKED
        elif metadata_pdu.pdu_header.trans_mode == TransmissionModes.ACKNOWLEDGED:
            self.states.state = CfdpStates.BUSY_CLASS_2_ACKED
        self._checksum_type = metadata_pdu.checksum_type
        self._closure_requested = metadata_pdu.closure_requested
        self._fp.file_name = Path(metadata_pdu.dest_file_name)
        self._fp.size = metadata_pdu.file_size
        self._transaction_id = TransactionId(
            source_entity_id=metadata_pdu.source_entity_id,
            transaction_seq_num=metadata_pdu.transaction_seq_num,
        )
        self.states.transaction = TransactionStep.RECEIVING_FILE_DATA
        msgs_to_user_list = None
        if metadata_pdu.options is not None:
            msgs_to_user_list = []
            for tlv in metadata_pdu.options:
                if tlv.tlv_type == TlvTypes.MESSAGE_TO_USER:
                    msgs_to_user_list.append(tlv)
        params = MetadataRecvParams(
            transaction_id=self._transaction_id,
            file_size=metadata_pdu.file_size,
            source_id=metadata_pdu.source_entity_id,
            dest_file_name=metadata_pdu.dest_file_name,
            source_file_name=metadata_pdu.source_file_name,
            msgs_to_user=msgs_to_user_list,
        )
        self.user.metadata_recv_indication(params=params)
        return True

    def state_machine(self) -> FsmResult:
        if self.states.state == CfdpStates.IDLE:
            transaction_was_started = False
            if DirectiveType.METADATA_PDU in self._file_directives_dict:
                for pdu in self._file_directives_dict.get(DirectiveType.METADATA_PDU):
                    metadata_pdu = PduHolder(pdu).to_metadata_pdu()
                    transaction_was_started = self._start_transaction(metadata_pdu)
                    if transaction_was_started:
                        break
            if not transaction_was_started:
                return FsmResult(self.states, self._pdu_holder)
        elif self.states.state == CfdpStates.BUSY_CLASS_1_NACKED:
            if self.states.transaction == TransactionStep.RECEIVING_FILE_DATA:
                # TODO: Sequence count check
                for file_data_pdu in self._file_data_deque:
                    data = file_data_pdu.file_data
                    offset = file_data_pdu.offset
                    if self.cfg.indication_cfg.file_segment_recvd_indication_required:
                        file_segment_indic_params = FileSegmentRecvParams(
                            transaction_id=self._transaction_id,
                            length=len(file_data_pdu.file_data),
                            offset=offset,
                            record_cont_state=file_data_pdu.record_continuation_state,
                            segment_metadata=file_data_pdu.segment_metadata,
                        )
                        self.user.file_segment_recv_indication(
                            params=file_segment_indic_params
                        )
                    self.user.vfs.write_data(self._fp.file_name, data, offset)
                eof_pdus = self._file_directives_dict.get(DirectiveType.EOF_PDU)
                if eof_pdus is not None:
                    for pdu in eof_pdus:
                        eof_pdu = PduHolder(pdu).to_eof_pdu()
                        self._handle_eof_pdu(eof_pdu)
            if self.states.transaction == TransactionStep.TRANSFER_COMPLETION:
                self._checksum_verify()
                self._notice_of_completion()
                self.states.transaction = TransactionStep.SENDING_FINISHED_PDU
            if self.states.transaction == TransactionStep.SENDING_FINISHED_PDU:
                self._prepare_finished_pdu()
        return FsmResult(self.states, self._pdu_holder)

    def pass_packet(self, packet: GenericPduPacket):
        # TODO: Sanity checks
        if packet.pdu_type == PduType.FILE_DATA:
            self._file_data_deque.append(cast(FileDataPdu, packet))
        else:
            if packet.directive_type in self._file_directives_dict:
                self._file_directives_dict.get(packet.directive_type).append(packet)
            else:
                self._file_directives_dict.update({packet.directive_type: [packet]})

    def confirm_packet_sent_advance_fsm(self):
        """Helper method which performs both :py:meth:`confirm_packet_sent` and
        :py:meth:`advance_fsm`
        """
        self.confirm_packet_sent()
        self.advance_fsm()

    def confirm_packet_sent(self):
        """Confirm that a packet generated by the :py:meth:`operation` was sent successfully"""
        self.states.packet_ready = False

    def advance_fsm(self):
        pass

    def _handle_eof_pdu(self, eof_pdu: EofPdu):
        # TODO: Error handling
        if eof_pdu.condition_code == ConditionCode.NO_ERROR:
            self._fp.crc32 = eof_pdu.file_checksum
            self._fp.size = eof_pdu.file_size
            if self.cfg.indication_cfg.eof_recv_indication_required:
                self.user.eof_recv_indication(self._transaction_id)
            if self.states.transaction == TransactionStep.RECEIVING_FILE_DATA:
                if self.states.state == CfdpStates.BUSY_CLASS_1_NACKED:
                    self.states.transaction = TransactionStep.TRANSFER_COMPLETION
                elif self.states.state == CfdpStates.BUSY_CLASS_2_ACKED:
                    self.states.transaction = TransactionStep.SENDING_ACK_PDU

    def _checksum_verify(self):
        pass

    def _notice_of_completion(self):
        pass

    def _prepare_finished_pdu(self):
        pass
