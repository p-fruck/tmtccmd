import dataclasses
from pathlib import Path
from typing import Optional
from crcmod.predefined import PredefinedCrc

from spacepackets.cfdp.pdu import PduHolder, EofPdu
from spacepackets.cfdp.pdu.file_data import FileDataPdu
from spacepackets.cfdp.pdu.finished import FileDeliveryStatus, DeliveryCode
from spacepackets.util import UnsignedByteField, ByteFieldGenerator
from tmtccmd.logging import get_console_logger
from tmtccmd.util import ProvidesSeqCount

from spacepackets.cfdp.pdu.metadata import MetadataPdu, MetadataParams
from spacepackets.cfdp.conf import PduConfig
from spacepackets.cfdp.defs import (
    ChecksumTypes,
    Direction,
    ConditionCode,
    TransmissionModes,
    NULL_CHECKSUM_U32,
)
from .defs import (
    BusyError,
    CfdpRequestType,
    SourceTransactionStep,
    CfdpStates,
    SourceStateWrapper,
    StateWrapper,
    TransactionId,
)
from .mib import LocalEntityCfg, RemoteEntityTable, RemoteEntityCfg
from .request import CfdpRequestWrapper, PutRequest
from .user import CfdpUserBase

LOGGER = get_console_logger()


class CfdpResult:
    def __init__(self):
        pass


@dataclasses.dataclass
class FileParams:
    offset = 0
    segment_len = 0
    crc32 = bytes()
    size = 0

    def reset(self):
        self.offset = 0
        self.segment_len = 0
        self.crc32 = bytes()
        self.size = 0


class TransferFieldWrapper:
    def __init__(self, local_entity_id: UnsignedByteField):
        self.transaction: Optional[TransactionId] = None
        self.fp = FileParams()
        self.remote_cfg: Optional[RemoteEntityCfg] = None
        self._pdu_conf = PduConfig.empty()
        self._pdu_conf.source_entity_id = local_entity_id

    @property
    def pdu_conf(self) -> PduConfig:
        return self._pdu_conf

    @property
    def source_id(self):
        return self._pdu_conf.source_entity_id

    @source_id.setter
    def source_id(self, source_id: UnsignedByteField):
        self._pdu_conf.source_entity_id = source_id

    @property
    def transmission_mode(self) -> TransmissionModes:
        return self._pdu_conf.trans_mode

    @transmission_mode.setter
    def transmission_mode(self, trans_mode: TransmissionModes):
        self._pdu_conf.trans_mode = trans_mode

    @property
    def transaction_seq_num(self) -> UnsignedByteField:
        return self._pdu_conf.transaction_seq_num

    @transaction_seq_num.setter
    def transaction_seq_num(self, seq_num: UnsignedByteField):
        self._pdu_conf.transaction_seq_num = seq_num

    def reset(self):
        self.fp.reset()
        self.remote_cfg = None
        self.transaction = None
        self._pdu_conf = PduConfig.empty()


class NoRemoteEntityCfgFound(Exception):
    pass


class SourceFileDoesNotExist(Exception):
    pass


class ChecksumNotImplemented(Exception):
    pass


class PacketSendNotConfirmed(Exception):
    pass


class FsmResult:
    def __init__(self, pdu_holder: PduHolder, states: SourceStateWrapper):
        self.pdu_holder = pdu_holder
        self.states = states


class CfdpSourceHandler:
    def __init__(
        self,
        cfg: LocalEntityCfg,
        seq_num_provider: ProvidesSeqCount,
        user: CfdpUserBase,
    ):
        self.states = SourceStateWrapper()
        self.pdu_wrapper = PduHolder(None)
        self.cfg = cfg
        self.user = user
        self.params = TransferFieldWrapper(cfg.local_entity_id)
        self.seq_num_provider = seq_num_provider
        self._current_req = CfdpRequestWrapper(None)

    @property
    def source_id(self) -> UnsignedByteField:
        return self.cfg.local_entity_id

    @source_id.setter
    def source_id(self, source_id: UnsignedByteField):
        self.cfg.local_entity_id = source_id
        self.params.source_id = source_id

    def start_transaction(
        self, wrapper: CfdpRequestWrapper, remote_cfg: RemoteEntityCfg
    ) -> bool:
        """Start a CFDP transaction.

        :param wrapper:
        :param remote_cfg:
        :return: Whether transaction was started successfully.
        """
        if wrapper.request_type == CfdpRequestType.PUT:
            if self.states.state != CfdpStates.IDLE:
                return False
            self._current_req = wrapper
            self.params.remote_cfg = remote_cfg
            self.states.packet_ready = False
            self._setup_transmission_mode()
            if self.params.transmission_mode == TransmissionModes.UNACKNOWLEDGED:
                self.states.state = CfdpStates.BUSY_CLASS_1_NACKED
            elif self.params.transmission_mode == TransmissionModes.ACKNOWLEDGED:
                self.states.state = CfdpStates.BUSY_CLASS_2_ACKED
            else:
                raise ValueError(
                    f"Invalid transmission mode {self.params.transmission_mode} passed"
                )
            return True

    def state_machine(self) -> FsmResult:
        """This is the primary state machine which performs the CFDP procedures like CRC calculation
        and PDU generation. The packets generated by this finite-state machine (FSM) need to be
        sent by the user using the"""
        if self.states.state == CfdpStates.IDLE:
            return FsmResult(self.pdu_wrapper, self.states)
        elif self.states.state == CfdpStates.BUSY_CLASS_1_NACKED:
            put_req = self._current_req.to_put_request()
            if self.states.step == SourceTransactionStep.IDLE:
                self.states.step = SourceTransactionStep.TRANSACTION_START
            if self.states.step == SourceTransactionStep.TRANSACTION_START:
                self._transaction_start(put_req)
                self.states.step = SourceTransactionStep.CRC_PROCEDURE
            if self.states.step == SourceTransactionStep.CRC_PROCEDURE:
                if self.params.fp.size == 0:
                    # Empty file, use null checksum
                    self.params.fp.crc32 = NULL_CHECKSUM_U32
                else:
                    self.params.fp.crc32 = self.calc_cfdp_file_crc(
                        crc_type=self.params.remote_cfg.crc_type,
                        file=put_req.cfg.source_file,
                        file_sz=self.params.fp.size,
                        segment_len=self.params.fp.segment_len,
                    )
                self.states.step = SourceTransactionStep.SENDING_METADATA
            if self.states.step == SourceTransactionStep.SENDING_METADATA:
                self._prepare_metadata_pdu(put_req)
                self.states.packet_ready = True
                return FsmResult(self.pdu_wrapper, self.states)
            if self.states.step == SourceTransactionStep.SENDING_FILE_DATA:
                if self._prepare_next_file_data_pdu(put_req):
                    self.states.packet_ready = True
                    return FsmResult(self.pdu_wrapper, self.states)
                else:
                    self.states.step = SourceTransactionStep.SENDING_EOF
            if self.states.step == SourceTransactionStep.SENDING_EOF:
                self._prepare_eof_pdu()
                self.states.packet_ready = True
                return FsmResult(self.pdu_wrapper, self.states)
            if self.states.step == SourceTransactionStep.NOTICE_OF_COMPLETION:
                self.user.transaction_finished_indication(
                    transaction_id=self.params.transaction,
                    condition_code=ConditionCode.NO_ERROR,
                    file_status=FileDeliveryStatus.FILE_STATUS_UNREPORTED,
                    delivery_code=DeliveryCode.DATA_COMPLETE,
                )
                # Transaction finished
                self.reset()
        return FsmResult(self.pdu_wrapper, self.states)

    def confirm_packet_sent_advance_fsm(self):
        """Helper method which performs both :py:method:`confirm_packet_sent` and
        :py:method:`advance_fsm`
        """
        self.confirm_packet_sent()
        self.advance_fsm()

    def confirm_packet_sent(self):
        """Confirm that a packet generated by the :py:method:`operation` was sent successfully"""
        self.states.packet_ready = False

    def advance_fsm(self):
        """Advance the internal FSM. This call is necessary to walk through the various steps
        of a CFDP transaction. This step is not done in the main :py:method:`operation` call
        because the packets generated by this method need to be sent first and then confirmed
        via the :py:method:`confirm_packet_sent` function.

        :return:
        """
        if self.states.packet_ready:
            raise PacketSendNotConfirmed(
                f"Must send current packet {self.pdu_wrapper.base} before "
                f"advancing state machine"
            )
        if self.states.state == CfdpStates.BUSY_CLASS_1_NACKED:
            if self.states.step == SourceTransactionStep.SENDING_METADATA:
                self.states.step = SourceTransactionStep.SENDING_FILE_DATA
            elif self.states.step == SourceTransactionStep.SENDING_FILE_DATA:
                if self.params.fp.offset == self.params.fp.size:
                    self.states.step = SourceTransactionStep.SENDING_EOF
            elif self.states.step == SourceTransactionStep.SENDING_EOF:
                self.user.eof_sent_indication(self.params.transaction)
                self.states.step = SourceTransactionStep.NOTICE_OF_COMPLETION

    def reset(self):
        self.states.step = SourceTransactionStep.IDLE
        self.states.state = CfdpStates.IDLE
        self.params.reset()

    def calc_cfdp_file_crc(
        self, crc_type: ChecksumTypes, file: Path, file_sz: int, segment_len: int
    ) -> bytes:
        if crc_type == ChecksumTypes.CRC_32:
            return self.calc_crc_for_file_crcmod(
                PredefinedCrc("crc32"), file, file_sz, segment_len
            )
        elif crc_type == ChecksumTypes.CRC_32C:
            return self.calc_crc_for_file_crcmod(
                PredefinedCrc("crc32c"), file, file_sz, segment_len
            )
        else:
            raise ChecksumNotImplemented(f"Checksum {crc_type} not implemented")

    def calc_crc_for_file_crcmod(
        self, crc_obj: PredefinedCrc, file: Path, file_sz: int, segment_len: int
    ):
        if not file.exists():
            # TODO: Handle this exception in the handler, reset CFDP state machine
            raise SourceFileDoesNotExist()
        current_offset = 0
        # Calculate the file CRC
        with open(file, "rb") as of:
            while True:
                if current_offset == file_sz:
                    break
                if file_sz < segment_len:
                    read_len = file_sz
                else:
                    next_offset = current_offset + segment_len
                    if next_offset > file_sz:
                        read_len = next_offset % file_sz
                    else:
                        read_len = segment_len
                if read_len > 0:
                    crc_obj.update(
                        self.user.vfs.read_from_opened_file(
                            of, current_offset, read_len
                        )
                    )
                current_offset += read_len
            return crc_obj.digest()

    def _setup_transmission_mode(self):
        put_req = self._current_req.to_put_request()
        # Transmission mode settings in the put request override settings from the remote MIB
        if put_req.cfg.trans_mode is not None:
            trans_mode_to_set = put_req.cfg.trans_mode
        else:
            trans_mode_to_set = self.params.remote_cfg.default_transmission_mode
        self.params.transmission_mode = trans_mode_to_set

    def _transaction_start(self, put_req: PutRequest):
        if not put_req.cfg.source_file.exists():
            # TODO: Handle this exception in the handler, reset CFDP state machine
            raise SourceFileDoesNotExist()
        self.params.fp.size = put_req.cfg.source_file.stat().st_size
        if self.params.remote_cfg is None:
            # It is actually not specified what to do if there is no remote configuration
            # for a given destination ID. I will treat this as a configuration error now
            # and raise an exception
            raise NoRemoteEntityCfgFound()
        self.params.fp.segment_len = self.params.remote_cfg.max_file_segment_len
        self.params.remote_cfg = self.params.remote_cfg
        self.params.transaction = TransactionId(
            source_entity_id=self.cfg.local_entity_id,
            transaction_seq_num=self._get_next_transfer_seq_num(),
        )
        self.user.transaction_indication(self.params.transaction)

    def _prepare_metadata_pdu(self, put_req: PutRequest):
        if self.states.packet_ready:
            raise PacketSendNotConfirmed(
                f"Must send current packet {self.pdu_wrapper.base} first"
            )
        self.params.pdu_conf.seg_ctrl = put_req.cfg.seg_ctrl
        self.params.pdu_conf.dest_entity_id = put_req.cfg.destination_id
        self.params.pdu_conf.crc_flag = self.params.remote_cfg.crc_on_transmission
        self.params.pdu_conf.direction = Direction.TOWARDS_RECEIVER
        self.params.pdu_conf.transaction_seq_num = self.params.transaction.seq_num
        params = MetadataParams(
            dest_file_name=put_req.cfg.dest_file,
            source_file_name=put_req.cfg.source_file.as_posix(),
            # TODO: These two can be overriden by PutRequest configuration
            checksum_type=self.params.remote_cfg.crc_type,
            closure_requested=self.params.remote_cfg.closure_requested,
            file_size=self.params.fp.size,
        )
        self.pdu_wrapper.base = MetadataPdu(
            pdu_conf=self.params.pdu_conf, params=params
        )

    def _prepare_next_file_data_pdu(self, request: PutRequest) -> bool:
        """Prepare the next file data PDU

        :param request:
        :return: True if a packet was prepared, False if PDU handling is done and the next steps
            in the Copy File procedure can be performed
        """
        # No need to send a file data PDU for an empty file
        if self.params.fp.size == 0:
            return False
        with open(request.cfg.source_file, "rb") as of:
            if self.params.fp.offset == self.params.fp.size:
                return False
            if self.states.packet_ready:
                raise PacketSendNotConfirmed(
                    f"Must send current packet {self.pdu_wrapper.base} first"
                )
            if self.params.fp.size < self.params.fp.segment_len:
                read_len = self.params.fp.size
            else:
                next_offset = self.params.fp.offset + self.params.fp.segment_len
                if next_offset > self.params.fp.size:
                    read_len = next_offset % self.params.fp.size
                else:
                    read_len = self.params.fp.segment_len
            file_data = self.user.vfs.read_from_opened_file(
                of, self.params.fp.offset, read_len
            )
            self.params.pdu_conf.transaction_seq_num = self._get_next_transfer_seq_num()
            # NOTE: Support for record continuation state not implemented yet. Segment metadata
            #       flag is therefore always set to False
            file_data_pdu = FileDataPdu(
                pdu_conf=self.params.pdu_conf,
                file_data=file_data,
                offset=self.params.fp.offset,
                segment_metadata_flag=False,
            )
            self.params.fp.offset += read_len
            self.pdu_wrapper.base = file_data_pdu
        return True

    def _prepare_eof_pdu(self):
        if self.states.packet_ready:
            raise PacketSendNotConfirmed(
                f"Must send current packet {self.pdu_wrapper.base} first"
            )
        self.pdu_wrapper.base = EofPdu(
            file_checksum=self.params.fp.crc32,
            file_size=self.params.fp.size,
            pdu_conf=self.params.pdu_conf,
        )

    def _get_next_transfer_seq_num(self) -> UnsignedByteField:
        next_seq_num = self.seq_num_provider.get_and_increment()
        if self.seq_num_provider.max_bit_width not in [8, 16, 32]:
            raise ValueError(
                "Invalid bit width for sequence number provider, must be one of [8, 16, 32]"
            )
        return ByteFieldGenerator.from_int(
            self.seq_num_provider.max_bit_width // 8, next_seq_num
        )


class CfdpRxHandler:
    pass


class CfdpHandler:
    def __init__(
        self,
        local_cfg: LocalEntityCfg,
        remote_cfg: RemoteEntityTable,
        seq_num_provider: ProvidesSeqCount,
        cfdp_user: CfdpUserBase,
    ):
        """

        :param local_cfg: Local entity configuration
        :param remote_cfg: Configuration table for remote entities
        :param cfdp_user: CFDP user which will receive indication messages and which also contains
            the virtual filestore implementation
        """
        # The ID is going to be constant after initialization, store in separately
        self.id = local_cfg.local_entity_id
        self.cfg = local_cfg
        self.remote_cfg_table = remote_cfg
        self.cfdp_user = cfdp_user
        self._tx_handler = CfdpSourceHandler(self.cfg, seq_num_provider, cfdp_user)
        self.state = StateWrapper(source_handler_state=self._tx_handler.states)
        self._request_wrapper = CfdpRequestWrapper(None)
        self._next_reception_pdu_wrapper = PduHolder(None)
        self._cfdp_result = CfdpResult()

    def state_machine(self) -> CfdpResult:
        """Perform the CFDP state machine. Primary function to call to generate new PDUs to send
        and to advance the internal state machine which also issues indications to the
        CFDP user.

        :raises SequenceNumberOverflow: Overflow of sequence number occurred. In this case, the
            number will be reset but no operation will occur and the state machine needs
            to be called again
        :raises NoRemoteEntityCfgFound: If no remote entity configuration for a given destination
            ID was found
        """
        if self.state != CfdpStates.IDLE:
            self._handle_transfer_state_machine()
            pass
        return self._cfdp_result

    def _handle_transfer_state_machine(self):
        if self._request_wrapper.request == CfdpRequestType.PUT:
            self._tx_handler.state_machine()

    def reset_transfer_state(self):
        pass
        # TODO: Implement
        # self.state.transfer_state = SorceState.IDLE
        # self._transfer_params.reset()

    def _prepare_finish_pdu(self):
        # TODO: Implement
        pass

    def pass_packet(self, raw_tm_packet: bytes):
        # TODO: Packet Handler
        pass

    @property
    def transfer_packet_ready(self):
        if self._tx_handler.pdu_wrapper.base is not None:
            return True
        return False

    @property
    def reception_packet_ready(self):
        if self._next_reception_pdu_wrapper.base is not None:
            return True
        return False

    @property
    def transfer_packet_wrapper(self) -> PduHolder:
        """Yield the next packet required to transfer a file"""
        return self._tx_handler.pdu_wrapper

    @property
    def reception_packet_wrapper(self) -> PduHolder:
        """Yield the next packed required to receive a file"""
        return self._next_reception_pdu_wrapper

    def start_put_request(self, put_request: PutRequest):
        """A put request initiates a copy procedure. For now, only one put request at a time
        is allowed"""
        if self.state.source_handler_state != CfdpStates.IDLE:
            raise BusyError(f"Currently in {self.state}, can not handle put request")
        self._request_wrapper.base = put_request
        remote_cfg = self.remote_cfg_table.get_remote_entity(
            put_request.cfg.destination_id
        )
        if remote_cfg is None:
            raise NoRemoteEntityCfgFound()
        self._tx_handler.start_transaction(
            remote_cfg=remote_cfg, wrapper=self._request_wrapper
        )
