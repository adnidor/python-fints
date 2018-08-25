import datetime
import logging
from decimal import Decimal
from contextlib import contextmanager
from collections import OrderedDict

from fints.segments.debit import HKDME, HKDSE
from mt940.models import Balance
from sepaxml import SepaTransfer

from .connection import FinTSHTTPSConnection
from .dialog import FinTSDialog, FinTSDialogOLD
from .formals import (
    KTI1, Account3, BankIdentifier,
    SynchronisationMode, TwoStepParametersCommon,
    TANMediaType2, TANMediaClass4,
)
from .message import FinTSInstituteMessage, FinTSMessageOLD
from .models import (
    SEPAAccount, TANChallenge, TANChallenge3,
    TANChallenge4, TANChallenge5, TANChallenge6,
)
from .security import (
    PinTanDummyEncryptionMechanism, PinTanOneStepAuthenticationMechanism,
    PinTanTwoStepAuthenticationMechanism,
)
from .segments import HIBPA3, HIRMG2, HIRMS2, HIUPA4, HIPINS1
from .segments.accounts import HISPA1, HKSPA, HKSPA1
from .segments.auth import HKTAB, HKTAN, HKTAB4, HKTAB5, HKTAN3, HKTAN5
from .segments.depot import HKWPD5, HKWPD6
from .segments.dialog import HISYN4, HKSYN3
from .segments.saldo import HKSAL5, HKSAL6, HKSAL7
from .segments.statement import HKKAZ5, HKKAZ6, HKKAZ7
from .segments.transfer import HKCCM1, HKCCS1
from .types import SegmentSequence
from .utils import MT535_Miniparser, Password, mt940_to_array, compress_datablob, decompress_datablob
from .parser import FinTS3Serializer

logger = logging.getLogger(__name__)

SYSTEM_ID_UNASSIGNED = '0'
DATA_BLOB_MAGIC = b'python-fints_DATABLOB'

class NeedRetryResponse:
    pass

class FinTS3Client:
    def __init__(self, bank_identifier, user_id, customer_id=None, set_data=None):
        self.accounts = []
        if isinstance(bank_identifier, BankIdentifier):
            self.bank_identifier = bank_identifier
        elif isinstance(bank_identifier, str):
            self.bank_identifier = BankIdentifier('280', bank_identifier)
        else:
            raise TypeError("bank_identifier must be BankIdentifier or str (BLZ)")
        self.system_id = SYSTEM_ID_UNASSIGNED
        self.user_id = user_id
        self.customer_id = customer_id or user_id
        self.bpd_version = 0
        self.bpa = None
        self.bpd = SegmentSequence()
        self.upd_version = 0
        self.upa = None
        self.upd = SegmentSequence()
        self.product_name = 'pyfints'
        self.product_version = '0.2'
        self.response_callbacks = []
        self._standing_dialog = None

        if set_data:
            self.set_data(set_data)

    def _new_dialog(self, lazy_init=False):
        raise NotImplemented()

    def _new_message(self, dialog: FinTSDialogOLD, segments, tan=None):
        raise NotImplemented()

    def _ensure_system_id(self):
        raise NotImplemented()

    def _process_response(self, segment, response):
        pass

    def _process_response_segments(self, message: FinTSInstituteMessage, internal_send=True):
        bpa = message.find_segment_first(HIBPA3)
        if bpa:
            self.bpa = bpa
            self.bpd_version = bpa.bpd_version
            self.bpd = SegmentSequence(
                message.find_segments(
                    callback = lambda m: len(m.header.type) == 6 and m.header.type[1] == 'I' and m.header.type[5] == 'S'
                )
            )

        upa = message.find_segment_first(HIUPA4)
        if upa:
            self.upa = upa
            self.upd_version = upa.upd_version
            self.upd = SegmentSequence(
                message.find_segments('HIUPD')
            )

        for seg in message.find_segments(HIRMG2):
            for response in seg.responses:
                if not internal_send:
                    self._log_response(None, response)

                    self._call_callbacks(None, response)

                self._process_response(None, response)

        for seg in message.find_segments(HIRMS2):
            for response in seg.responses:
                segment = None # FIXME: Provide segment

                if not internal_send:
                    self._log_response(segment, response)

                    self._call_callbacks(segment, response)

                self._process_response(segment, response)

    def process_dialog_response(self, message: FinTSInstituteMessage):
        self._process_response_segments(message, internal_send=True)

    def _send(self, dialog, *segments):
        "Internal send, may be overriden in subclasses"
        response = dialog.send(*segments)
        self._process_response_segments(response, internal_send=False)
        return response

    def __enter__(self):
        if self._standing_dialog:
            raise Exception("Cannot double __enter__() {}".format(self))
        self._standing_dialog = self._get_dialog()
        self._standing_dialog.__enter__()

    def __exit__(self, exc_type, exc_value, traceback):
        if self._standing_dialog:
            self._standing_dialog.__exit__(exc_type, exc_value, traceback)
        else:
            raise Exception("Cannot double __exit__() {}".format(self))

        self._standing_dialog = None

    def _get_dialog(self, lazy_init=False):
        if lazy_init and self._standing_dialog:
            raise Exception("Cannot _get_dialog(lazy_init=True) with _standing_dialog")

        if self._standing_dialog:
            return self._standing_dialog

        if not lazy_init:
            self._ensure_system_id()

        return self._new_dialog(lazy_init=lazy_init)

    def _set_data_v1(self, data):
        self.system_id = data.get('system_id', self.system_id)

        if all(x in data for x in ('bpd_bin', 'bpa_bin', 'bpd_version')):
            if data['bpd_version'] >= self.bpd_version and data['bpa_bin']:
                self.bpd = SegmentSequence(data['bpd_bin'])
                self.bpa = SegmentSequence(data['bpa_bin']).segments[0]
                self.bpd_version = data['bpd_version']

        if all(x in data for x in ('upd_bin', 'upa_bin', 'upd_version')):
            if data['upd_version'] >= self.upd_version and data['upa_bin']:
                self.upd = SegmentSequence(data['upd_bin'])
                self.upa = SegmentSequence(data['upa_bin']).segments[0]
                self.upd_version = data['upd_version']

    def _get_data_v1(self, including_private=False):
        data = {
            "system_id": self.system_id,
            "bpd_bin": self.bpd.render_bytes(),
            "bpa_bin": FinTS3Serializer().serialize_message(self.bpa) if self.bpa else None,
            "bpd_version": self.bpd_version,
        }

        if including_private:
            data.update({
                "upd_bin": self.upd.render_bytes(),
                "upa_bin": FinTS3Serializer().serialize_message(self.upa) if self.upa else None,
                "upd_version": self.upd_version,
            })

        return data

    def get_data(self, including_private=False):
        # FIXME Test, document
        data = self._get_data_v1(including_private=including_private)
        return compress_datablob(DATA_BLOB_MAGIC, 1, data)

    def set_data(self, blob):
        # FIXME Test, document
        decompress_datablob(DATA_BLOB_MAGIC, self, blob)

    def _log_response(self, segment, response):
        if response.code[0] in ('0', '1'):
            log_target = logger.info
        elif response.code[0] in ('3'):
            log_target = logger.warning
        else:
            log_target = logger.error

        log_target("Dialog response: {} - {}{}".format(
            response.code,
            response.text,
            " ({!r})".format(response.parameters) if response.parameters else "")
        )

    def get_sepa_accounts(self):
        """
        Returns a list of SEPA accounts

        :return: List of SEPAAccount objects.
        """

        with self._get_dialog() as dialog:
            response = self._send(dialog, HKSPA1())
            
        self.accounts = []
        for seg in response.find_segments(HISPA1):
            self.accounts.extend(seg.accounts)

        return [a for a in [acc.as_sepa_account() for acc in self.accounts] if a]

    def _fetch_with_touchdowns(self, dialog, segment_factory, *args, **kwargs):
        """Execute a sequence of fetch commands on dialog.
        segment_factory must be a callable with one argument touchdown. Will be None for the
        first call and contains the institute's touchdown point on subsequent calls.
        segment_factory must return a command segment.
        Extra arguments will be passed to FinTSMessage.response_segments.
        Return value is a concatenated list of the return values of FinTSMessage.response_segments().
        """
        responses = []
        touchdown_counter = 1
        touchdown = None

        while touchdown or touchdown_counter == 1:
            seg = segment_factory(touchdown)

            rm = self._send(dialog, seg)

            for resp in rm.response_segments(seg, *args, **kwargs):
                responses.append(resp)

            touchdown = None
            for response in rm.responses(seg, '3040'):
                touchdown = response.parameters[0]
                break

            if touchdown:
                logger.info('Fetching more results ({})...'.format(touchdown_counter))

            touchdown_counter += 1

        return responses

    def _find_highest_supported_command(self, *segment_classes, **kwargs):
        """Search the BPD for the highest supported version of a segment."""
        return_parameter_segment = kwargs.get("return_parameter_segment", False)

        parameter_segment_name = "{}I{}S".format(segment_classes[0].TYPE[0], segment_classes[0].TYPE[2:])
        version_map = dict((clazz.VERSION, clazz) for clazz in segment_classes)
        max_version = self.bpd.find_segment_highest_version(parameter_segment_name, version_map.keys())
        if not max_version:
            raise ValueError('No supported {} version found. I support {}, bank supports {}.'.format(
                parameter_segment_name,
                tuple(version_map.keys()),
                tuple(v.header.version for v in self.bpd.find_segments(parameter_segment_name))
            ))

        if return_parameter_segment:
            return max_version, version_map.get(max_version.header.version)
        else:
            return version_map.get(max_version.header.version)


    def get_statement(self, account: SEPAAccount, start_date: datetime.date, end_date: datetime.date):
        """
        Fetches the statement of a bank account in a certain timeframe.

        :param account: SEPA
        :param start_date: First day to fetch
        :param end_date: Last day to fetch
        :return: A list of mt940.models.Transaction objects
        """

        with self._get_dialog() as dialog:
            hkkaz = self._find_highest_supported_command(HKKAZ5, HKKAZ6, HKKAZ7)

            logger.info('Start fetching from {} to {}'.format(start_date, end_date))
            responses = self._fetch_with_touchdowns(
                dialog,
                lambda touchdown: hkkaz(
                    account=hkkaz._fields['account'].type.from_sepa_account(account),
                    all_accounts=False,
                    date_start=start_date,
                    date_end=end_date,
                    touchdown_point=touchdown,
                ),
                'HIKAZ'
            )
            logger.info('Fetching done.')

        statement = []
        for seg in responses:
            ## FIXME What is the encoding of MT940 messages?
            statement += mt940_to_array(seg.statement_booked.decode('iso-8859-1'))

        logger.debug('Statement: {}'.format(statement))

        return statement

    def get_balance(self, account: SEPAAccount):
        """
        Fetches an accounts current balance.

        :param account: SEPA account to fetch the balance
        :return: A mt940.models.Balance object
        """

        with self._get_dialog() as dialog:
            hksal = self._find_highest_supported_command(HKSAL5, HKSAL6, HKSAL7)

            seg = hksal(
                account=hksal._fields['account'].type.from_sepa_account(account),
                all_accounts=False,
            )

            response = self._send(dialog, seg)

            for resp in response.response_segments(seg, 'HISAL'):
                return resp.balance_booked.as_mt940_Balance()

    def get_holdings(self, account: SEPAAccount):
        """
        Retrieve holdings of an account.

        :param account: SEPAAccount to retrieve holdings for.
        :return: List of Holding objects
        """
        # init dialog
        with self._get_dialog() as dialog:
            hkwpd = self._find_highest_supported_command(HKWPD5, HKWPD6)

            seg = hkwpd(
                account=hkwpd._fields['account'].type.from_sepa_account(account),
            )

            response = self._send(dialog, seg)

            for resp in response.response_segments(seg, 'HIWPD'):
                ## FIXME BROKEN
                mt535_lines = str.splitlines(resp)
                # The first line contains a FinTS HIWPD header - drop it.
                del mt535_lines[0]
                mt535 = MT535_Miniparser()
                return mt535.parse(mt535_lines)

            logger.debug('No HIWPD response segment found - maybe account has no holdings?')
            return []

    def start_simple_sepa_transfer(self, account: SEPAAccount, iban: str, bic: str,
                                   recipient_name: str, amount: Decimal, account_name: str, reason: str,
                                   endtoend_id='NOTPROVIDED'):
        """
        Start a simple SEPA transfer.

        :param account: SEPAAccount to start the transfer from.
        :param tan_method: TANMethod object to use.
        :param iban: Recipient's IBAN
        :param bic: Recipient's BIC
        :param recipient_name: Recipient name
        :param amount: Amount as a ``Decimal``
        :param account_name: Sender account name
        :param reason: Transfer reason
        :param endtoend_id: End-to-end-Id (defaults to ``NOTPROVIDED``)
        :param tan_description: TAN medium description (if required)
        :return: Returns a TANChallenge object
        """
        config = {
            "name": account_name,
            "IBAN": account.iban,
            "BIC": account.bic,
            "batch": False,
            "currency": "EUR",
        }
        sepa = SepaTransfer(config, 'pain.001.001.03')
        payment = {
            "name": recipient_name,
            "IBAN": iban,
            "BIC": bic,
            "amount": int(Decimal(amount) * 100),  # in cents
            "execution_date": datetime.date(1999, 1, 1),
            "description": reason,
            "endtoend_id": endtoend_id,
        }
        sepa.add_payment(payment)
        xml = sepa.export()
        return self.start_sepa_transfer(account, xml)

    def start_sepa_transfer(self, account: SEPAAccount, pain_message: bytes, multiple=False,
                            control_sum=None, currency='EUR', book_as_single=False,
                            pain_descriptor='urn:iso:std:iso:20022:tech:xsd:pain.001.001.03'):
        """
        Start a custom SEPA transfer.

        :param account: SEPAAccount to send the transfer from.
        :param pain_message: SEPA PAIN message containing the transfer details.
        :param multiple: Whether this message contains multiple transfers.
        :param control_sum: Sum of all transfers (required if there are multiple)
        :param currency: Transfer currency
        :param book_as_single: Kindly ask the bank to put multiple transactions as separate lines on the bank statement (defaults to ``False``)
        :return: Returns a TANChallenge object
        """

        with self._get_dialog() as dialog:
            if multiple:
                command_class = HKCCM1
            else:
                command_class = HKCCS1

            hiccxs, hkccx = self._find_highest_supported_command(
                HKCCM1 if multiple else HKCCS1,
                return_parameter_segment=True
            )

            seg = hkccx(
                account=hkccx._fields['account'].type.from_sepa_account(account),
                sepa_descriptor=pain_descriptor,
                sepa_pain_message=pain_message,
            )

            if multiple:
                if hiccxs.parameter.sum_amount_required and not control_sum:
                    raise ValueError("Control sum required.")
                if book_as_single and not hiccxs.parameter.single_booking_allowed:
                    # FIXME Only do a warning and fall-back to book_as_single=False?
                    raise ValueError("Single booking not allowed by bank.")

                if control_sum:
                    seg.sum_amount.amount = control_sum
                    seg.sum_amount.currency = currency

                if book_as_single:
                    seg.request_single_booking = True

            response = self._send(dialog, seg)

            if isinstance(response, NeedRetryResponse):
                return response

            # FIXME Properly find return code
            return True

    def _get_start_sepa_debit_message(self, dialog, account: SEPAAccount, pain_message: str, tan_method,
                                      tan_description, multiple, control_sum, currency, book_as_single):
        if multiple:
            if not control_sum:
                raise ValueError("Control sum required.")
            segreq = HKDME(3, account, pain_message, control_sum, currency, book_as_single)
        else:
            segreq = HKDSE(3, account, pain_message)
        segtan = HKTAN(4, '4', '', tan_description, tan_method.version)
        return self._new_message(dialog, [
            segreq,
            segtan
        ])

    def start_sepa_debit(self, account: SEPAAccount, pain_message: str, tan_method, tan_description='',
                         multiple=False, control_sum=None, currency='EUR', book_as_single=False):
        """
        Start a custom SEPA debit.

        :param account: SEPAAccount to send the debit from.
        :param pain_message: SEPA PAIN message containing the debit details.
        :param tan_method: TANMethod object to use.
        :param tan_description: TAN medium description (if required)
        :param multiple: Whether this message contains multiple debits.
        :param control_sum: Sum of all debits (required if there are multiple)
        :param currency: Debit currency
        :param book_as_single: Kindly ask the bank to put multiple transactions as separate lines on the bank statement (defaults to ``False``)
        :return: Returns a TANChallenge object
        """
        dialog = self._get_dialog()
        dialog.sync()
        dialog.tan_mechs = [tan_method]
        dialog.init()

        with self.pin.protect():
            logger.debug('Sending: {}'.format(self._get_start_sepa_debit_message(
                dialog, account, pain_message, tan_method, tan_description, multiple, control_sum, currency,
                book_as_single
            )))

        resp = self._send(dialog, self._get_start_sepa_debit_message(
            dialog, account, pain_message, tan_method, tan_description, multiple, control_sum, currency,
            book_as_single
        ))
        logger.debug('Got response: {}'.format(resp))
        return self._tan_requiring_response(dialog, resp)

    def add_response_callback(self, cb):
        # FIXME document
        self.response_callbacks.append(cb)

    def remove_response_callback(self, cb):
        # FIXME document
        self.response_callbacks.remove(cb)

    def set_product(self, product_name, product_version):
        """Set the product name and version that is transmitted as part of our identification
        
        According to 'FinTS Financial Transaction Services, Schnittstellenspezifikation, Formals',
        version 3.0, section C.3.1.3, you should fill this with useful information about the
        end-user product, *NOT* the FinTS library."""

        self.product_name = product_name
        self.product_version = product_version

    def _call_callbacks(self, *cb_data):
        for cb in self.response_callbacks:
            cb(*cb_data)

    def pause_dialog(self):
        """Pause a standing dialog and return the saved dialog state.

        Sometimes, for example in a web app, it's not possible to keep a context open
        during user input. In some cases, though, it's required to send a response
        within the same dialog that issued the original task (f.e. TAN with TANTimeDialogAssociation.NOT_ALLOWED).
        This method freezes the current standing dialog (started with FinTS3Client.__enter__()) and
        returns the frozen state.

        Commands MUST NOT be issued in the dialog after calling this method.

        MUST be used in conjunction with get_data()/set_data().

        Caller SHOULD ensure that the dialog is resumed (and properly ended) within a reasonable amount of time.

        :Example:
            client = FinTS3PinTanClient(..., set_data=None)
            with client:
                challenge = client.start_sepa_transfer(...)

                dialog_data = client.pause_dialog()

                # dialog is now frozen, no new commands may be issued
                # exiting the context does not end the dialog

            client_data = client.get_data()

            # Store dialog_data and client_data out-of-band somewhere
            # ... Some time passes ...
            # Later, possibly in a different process, restore the state

            client = FinTS3PinTanClient(..., set_data=client_data)
            with client.resume_dialog(dialog_data):
                client.send_tan(...)

                # Exiting the context here ends the dialog, unless frozen with pause_dialog() again.
        """
        if not self._standing_dialog:
            raise Exception("Cannot pause dialog, no standing dialog exists")
        return self._standing_dialog.pause()

    @contextmanager
    def resume_dialog(self, dialog_data):
        # FIXME document, test,    NOTE NO UNTRUSTED SOURCES
        if self._standing_dialog:
            raise Exception("Cannot resume dialog, existing standing dialog")
        self._standing_dialog = FinTSDialog.create_resume(self, dialog_data)
        with self._standing_dialog:
            yield self
        self._standing_dialog = None

class NeedTANResponse(NeedRetryResponse):
    def __init__(self, command_seg, hitan):
        self.command_seg = command_seg
        self.hitan = hitan

class FinTS3PinTanClient(FinTS3Client):

    def __init__(self, bank_identifier, user_id, pin, server, customer_id=None, *args, **kwargs):
        self.pin = Password(pin)
        self._pending_tan = None
        self.connection = FinTSHTTPSConnection(server)
        self.allowed_security_functions = []
        self.selected_security_function = None
        self.selected_tan_medium = None
        super().__init__(bank_identifier=bank_identifier, user_id=user_id, customer_id=customer_id, *args, **kwargs)

    def _new_dialog(self, lazy_init=False):
        if not self.selected_security_function or self.selected_security_function == '999':
            enc = PinTanDummyEncryptionMechanism(1)
            auth = PinTanOneStepAuthenticationMechanism(self.pin)
        else:
            enc = PinTanDummyEncryptionMechanism(2)
            auth = PinTanTwoStepAuthenticationMechanism(
                self,
                self.selected_security_function,
                self.pin,
            )

        return FinTSDialog(self, 
            lazy_init=lazy_init,
            enc_mechanism=enc,
            auth_mechanisms=[auth],
        )

    def _ensure_system_id(self):
        if self.system_id != SYSTEM_ID_UNASSIGNED:
            return

        with self._get_dialog(lazy_init=True) as dialog:
            response = dialog.init(
                HKSYN3(SynchronisationMode.NEW_SYSTEM_ID),
            )
            
        seg = response.find_segment_first(HISYN4)
        if not seg:
            raise ValueError('Could not find system_id')
        self.system_id = seg.system_id

    def _set_data_v1(self, data):
        super()._set_data_v1(data)
        self.selected_tan_medium = data.get('selected_tan_medium', self.selected_tan_medium)
        self.selected_security_function = data.get('selected_security_function', self.selected_security_function)
        self.allowed_security_functions = data.get('allowed_security_functions', self.allowed_security_functions)

    def _get_data_v1(self, including_private=False):
        data = super()._get_data_v1(including_private=including_private)
        data.update({
            "selected_security_function": self.selected_security_function,
            "selected_tan_medium": self.selected_tan_medium,
        })

        if including_private:
            data.update({
                "allowed_security_functions": self.allowed_security_functions,
            })

        return data

    def _get_tan_segment(self, orig_seg, tan_process, tan_seg=None):
        tan_mechanism = self.get_tan_mechanisms()[self.get_current_tan_mechanism()]

        hitans = self.bpd.find_segment_first('HITANS', tan_mechanism.VERSION)
        hktan = {
            3: HKTAN3,
            5: HKTAN5,
        }.get(tan_mechanism.VERSION)

        seg = hktan(tan_process=tan_process)

        if tan_process == '1':
            seg.segment_type = orig_seg.header.type
            account_ = getattr(orig_seg, 'account', None)
            if isinstance(account, KTI1):
                seg.account = account
            raise NotImplementedError("TAN-Process 1 not implemented")

        if tan_process in ('1', '3', '4') and \
            tan_mechanism.supported_media_number > 1 and \
            tan_mechanism.description_required == DescriptionRequired.MUST:
                seg.tan_medium_name = self.selected_tan_medium

        if tan_process in ('2', '3'):
            seg.task_reference = tan_seg.task_reference

        if tan_process in ('1', '2'):
            seg.further_tan_follows = False

        return seg

    def _need_twostep_tan_for_segment(self, seg):
        if not self.selected_security_function or self.selected_security_function == '999':
            return False
        else:
            hipins = self.bpd.find_segment_first(HIPINS1)
            if not hipins:
                return False
            else:
                for requirement in hipins.parameter.transaction_tans_required:
                    if seg.header.type == requirement.transaction:
                        return requirement.tan_required

        return False

    def _send(self, dialog, *segments):
        need_twostep = any(self._need_twostep_tan_for_segment(seg) for seg in segments)

        with dialog:
            if need_twostep:
                assert len(segments) == 1
                seg = segments[0]
                tan_seg = self._get_tan_segment(seg, '4')

                response = super()._send(dialog, seg, tan_seg)

                for resp in response.responses(tan_seg):
                    if resp.code == '0030':
                        response = NeedTANResponse(seg, response.find_segment_first('HITAN'))

            else:
                response = super()._send(dialog, *segments)

        return response


    def send_tan(self, challenge: NeedTANResponse, tan: str):
        """
        Sends a TAN to confirm a pending operation.

        :param challenge: NeedTANResponse to respond to
        :param tan: TAN value
        :return: Currently no response
        """

        with self._get_dialog() as dialog:
            tan_seg = self._get_tan_segment(challenge.command_seg, '2', challenge.hitan)
            self._pending_tan = tan

            response = self._send(dialog, tan_seg)

            # FIXME Try to return a better return code

        return response

    def _process_response(self, segment, response):
        if response.code == '3920':
            self.allowed_security_functions = list(response.parameters)
            if self.selected_security_function is None or not self.selected_security_function in self.allowed_security_functions:
                # Select the first available twostep security_function that we support
                for security_function, parameter in self.get_tan_mechanisms().items():
                    if security_function == '999':
                        # Skip onestep TAN
                        continue
                    if parameter.tan_process != '2':
                        # Only support process variant 2 for now
                        continue
                    try:
                        self.set_tan_mechanism(parameter.security_function)
                        break
                    except NotImplementedError:
                        pass
                else:
                    # Fall back to onestep
                    self.set_tan_mechanism('999')

    def get_tan_mechanisms(self):
        """
        Get the available TAN mechanisms.

        :return: Dictionary of security_function: TwoStepParameters[1-5] objects.
        """

        retval = OrderedDict()

        for version in range(1, 6):
            for seg in self.bpd.find_segments('HITANS', version):
                for parameter in seg.parameter.twostep_parameters:
                    if parameter.security_function in self.allowed_security_functions:
                        retval[parameter.security_function] = parameter

        return retval

    def get_current_tan_mechanism(self):
        return self.selected_security_function

    def set_tan_mechanism(self, security_function):
        if self._standing_dialog:
            raise Exception("Cannot change TAN mechanism with a standing dialog")
        self.selected_security_function = security_function

    def set_tan_medium(self, tan_medium):
        if self._standing_dialog:
            raise Exception("Cannot change TAN medium with a standing dialog")
        self.selected_tan_medium = tan_medium

    def get_tan_media(self, media_type = TANMediaType2.ALL, media_class = TANMediaClass4.ALL):
        """Get information about TAN lists/generators.

        Returns tuple of fints.formals.TANUsageOption and a list of fints.formals.TANMedia4 or fints.formals.TANMedia5 objects."""

        with self._get_dialog() as dialog:
            hktab = self._find_highest_supported_command(HKTAB4, HKTAB5)

            seg = hktab(
                tan_media_type = media_type,
                tan_media_class = str(media_class),
            )

            response = self._send(dialog, seg)

            for resp in response.response_segments(seg, 'HITAB'):
                return resp.tan_usage_option, list(resp.tan_media_list)

