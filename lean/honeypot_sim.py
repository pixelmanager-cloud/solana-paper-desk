"""L22: honeypot sell-simulation ENTRY FILTER. Before the entry quotes of a candidate that is NOT a plain pump-graduated SPL mint,
simulate an actual SELL by a real current holder and reject the candidate when the token itself refuses to be sold.

PAPER ONLY. Nothing here signs, sends or broadcasts. The transaction is compiled UNSIGNED (one all-zero signature slot) and
only ever passed to ``simulateTransaction`` with ``sigVerify: false`` and ``replaceRecentBlockhash: true``: the RPC node executes
it against a copy of its bank and discards the result; a simulation can never land on chain. ``lean.providers.Helius.rpc``
refuses any other simulateTransaction shape (and any transaction carrying a non-zero signature) before any I/O.

Why: Solana honeypots are almost always Token-2022 (a transfer hook that rejects outbound transfers, a 99-100 % transfer fee, a
permanent delegate, default-frozen accounts) or a live freeze authority. A pump.fun-graduated legacy SPL mint whose mint and
freeze authorities are revoked cannot be a token-level honeypot, so it is never simulated (``SKIPPED_STANDARD_SPL``, no call).
Static mint checks cannot see what a transfer hook program will do at run time; a simulated sell can.

Scope (from what the screen already loaded; no request): Token-2022 mints, any mint with extensions or a live authority, and
any candidate from a non-pump source are simulated. Everything else is ``SKIPPED_STANDARD_SPL``.

Simulation (MAIN lane, it gates entries; hard cap ``max_per_hour`` simulated candidates, persisted through the store):
  1. a real current holder: the screen's own ``getTokenLargestAccounts`` rows (one call only when the screen did not keep them),
     without the pool vault and the burn accounts, resolved to their owners with ONE ``getMultipleAccounts``; owners that are the
     coin creator, a burn address or off-curve (a PDA cannot pay a fee) are skipped. Largest first.
  2. Jupiter builds a SELL of a small amount (a tenth of our paper size, capped by the holder's balance) with ``taker = holder``
     on the same ``/swap/v2/build`` endpoint the lean quote uses; the instructions are compiled into a v0 message with the
     address lookup tables Jupiter returned (no tip instruction), payer = holder, null signature.
  3. ``simulateTransaction``; the outcome is mapped (``classify``):
       success                                                        -> SELL_SIM_OK
       the token program fails in the swap (a non-benign code), a
       program invoked BY the token program fails (a transfer hook), or
       the logs say the account is frozen                             -> SELL_SIM_BLOCKED (with a logs excerpt)
       fee payer cannot pay / missing / blockhash problem             -> retry ONCE with the next holder, then SELL_SIM_INCONCLUSIVE
       anything else (slippage, setup, RPC/transport failure, cap)    -> SELL_SIM_INCONCLUSIVE
       Jupiter cannot build the sell                                  -> SELL_SIM_NO_BUILD
  4. when the small sell is OK and that holder holds at least our full paper size, the full size is simulated too (dynamic
     honeypots can be size-dependent); a BLOCKED full-size sell makes the candidate BLOCKED.

Filter: SELL_SIM_BLOCKED is a HAZARD reject (entry SKIP ``HONEYPOT_SIM_BLOCKED``; not a soft reason, so the watchlist never
re-screens it). INCONCLUSIVE / NO_BUILD for a Token-2022 mint with a transfer hook or a transfer fee above 10 % (or whose mint
bytes cannot be read) is also a reject (``HONEYPOT_SIM_UNVERIFIED_RISKY``). Any other outcome is recorded and the candidate
continues to the normal entry path unchanged. Every handled in-scope or skipped candidate gets ONE
``observations(kind='honeypot_sim')`` row whose bytes are the JSON record and whose meta carries ``decision_ts`` (the time the
outcome was decided: every piece of evidence in the row was fetched before it).

Never touches the exit loop: the only caller is the runner's candidate path, before the entry quotes.
"""
import base64
import json
import sqlite3
import time
from collections import deque

from lean.providers import ProviderError, SOL_MINT
from lean.store import StoreError

KIND = 'honeypot_sim'
OK, BLOCKED, INCONCLUSIVE, NO_BUILD = 'SELL_SIM_OK', 'SELL_SIM_BLOCKED', 'SELL_SIM_INCONCLUSIVE', 'SELL_SIM_NO_BUILD'
SKIPPED = 'SKIPPED_STANDARD_SPL'
RETRY = 'RETRY'                                   # internal: the holder could not pay / blockhash; try the next holder once
REJECT_BLOCKED = 'HONEYPOT_SIM_BLOCKED'
REJECT_RISKY = 'HONEYPOT_SIM_UNVERIFIED_RISKY'
RISKY_FEE_BPS = 1000                              # a transfer fee above 10 % makes an unverified sell a reject
TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022})
DEFAULT_SOURCE = 'pump_graduation'
BURN_OWNERS = frozenset({'1nc1nerator11111111111111111111111111111111', '11111111111111111111111111111111'})
MAX_HOLDER_ROWS = 6                               # holder token accounts resolved in the ONE getMultipleAccounts
MAX_TX_BYTES = 1232
MAX_LOG_LINES = 16
MAX_LOG_CHARS = 200
# Transaction-level errors that are about the payer (the holder we borrowed), not the token: try another holder once.
RETRY_ERRORS = frozenset({'InsufficientFundsForFee', 'AccountNotFound', 'BlockhashNotFound', 'InsufficientFundsForRent',
                          'InvalidAccountForFee'})
# SPL / Token-2022 error codes that a normal sell can hit for reasons unrelated to the token's transfer rules (0 NotRentExempt,
# 1 InsufficientFunds, 2 InvalidMint, 3 MintMismatch, 4 OwnerMismatch, 9 UninitializedState, 12 InvalidInstruction,
# 18 MintDecimalsMismatch). Every OTHER token-program code in the swap (17 AccountFrozen, NonTransferable, a paused mint, a
# hook-account mismatch, ...) is the token refusing the transfer: BLOCKED.
BENIGN_TOKEN_CODES = frozenset({0, 1, 2, 3, 4, 9, 12, 18})
TOKEN_INSUFFICIENT_FUNDS = 1
# Token-2022 extension tags that make a mint "risky" when its sell cannot be verified.
EXT_TRANSFER_FEE, EXT_TRANSFER_HOOK = 1, 14
CONFIG_KEYS = ('enabled', 'max_per_hour', 'full_size')


class HoneypotConfigError(ValueError):
    pass


def config(raw):
    """Strict: None / {} = off. ``{"enabled": bool, "max_per_hour": 1..3600 (30), "full_size": bool (true)}``."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise HoneypotConfigError('honeypot_sim must be an object')
    unknown = set(raw) - set(CONFIG_KEYS) - {'_comment'}
    if unknown:
        raise HoneypotConfigError('unknown honeypot_sim keys: %s' % sorted(unknown))
    out = {'enabled': raw.get('enabled', False), 'max_per_hour': raw.get('max_per_hour', 30), 'full_size': raw.get('full_size', True)}
    for key in ('enabled', 'full_size'):
        if not isinstance(out[key], bool):
            raise HoneypotConfigError('honeypot_sim.%s must be true or false' % key)
    if type(out['max_per_hour']) is not int or not 1 <= out['max_per_hour'] <= 3600:
        raise HoneypotConfigError('honeypot_sim.max_per_hour must be an integer in 1..3600')
    return out


def build(raw, store, *, clock=time.time):
    """The filter, or None when it is off (absent key = today's behaviour)."""
    cfg = config(raw)
    return HoneypotSim(cfg, store, clock=clock) if cfg['enabled'] else None


# ------------------------------------------------------------------------------------------------- pure helpers
def _raw_body(screen, label):
    for name, body in getattr(screen, 'raw', ()) or ():
        if name == label:
            return body
    return None


def mint_account(screen):
    """``(bytes, owner program)`` of the mint account the screen kept (``accounts_mint_pool``, first value), or (None, None)."""
    body = _raw_body(screen, 'accounts_mint_pool')
    try:
        parsed = json.loads(body)
        value = (parsed.get('result', parsed) or {})['value'][0]
        return base64.b64decode(value['data'][0], validate=True), value.get('owner')
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None, None


def mint_facts(data):
    """``{'extensions': [tag, ...], 'transfer_fee_bps': int, 'mint_authority': bool, 'freeze_authority': bool}`` from raw mint
    bytes (legacy 82 bytes or Token-2022 with TLV extensions). The transfer fee is the larger of the older and newer bps.
    Raises ValueError when the bytes are not a readable mint."""
    if not isinstance(data, (bytes, bytearray)) or len(data) < 82:
        raise ValueError('mint bytes')
    facts = {'extensions': [], 'transfer_fee_bps': 0, 'legacy_layout': len(data) == 82, 'mint_authority': int.from_bytes(data[0:4], 'little') != 0,
             'freeze_authority': int.from_bytes(data[46:50], 'little') != 0}
    if len(data) == 82:
        return facts
    if len(data) < 166 or data[165] != 1:
        raise ValueError('not a Token-2022 mint')
    offset = 166
    while offset + 4 <= len(data):
        tag, size = int.from_bytes(data[offset:offset + 2], 'little'), int.from_bytes(data[offset + 2:offset + 4], 'little')
        offset += 4
        if tag == 0 and size == 0:
            break
        if offset + size > len(data):
            raise ValueError('truncated extension')
        facts['extensions'].append(tag)
        if tag == EXT_TRANSFER_FEE:
            if size != 108:
                raise ValueError('transfer fee config length')
            older = int.from_bytes(data[offset + 88:offset + 90], 'little')        # epoch u64, max fee u64, bps u16 (x2)
            newer = int.from_bytes(data[offset + 106:offset + 108], 'little')
            facts['transfer_fee_bps'] = max(older, newer)
        offset += size
    return facts


def scope(screen, owner_program, source_is_pump):
    """Pure: is this candidate simulated, and is it 'risky' (an unverified sell rejects it)? The owner program is the one of the
    retained mint account (the screen's ``token_program`` feature when it was not retained)."""
    data, owner = mint_account(screen)
    owner_program = owner or owner_program
    try:
        facts = mint_facts(data) if data is not None else None
    except ValueError:
        facts = None
    out = {'token_program': owner_program, 'source_is_pump': bool(source_is_pump), 'mint_readable': facts is not None}
    if facts is None:
        # unreadable mint evidence fails closed: simulated, and an unverified sell rejects it
        return dict(out, in_scope=True, risky=True, extensions=None, transfer_fee_bps=None)
    plain = (owner_program == TOKEN_PROGRAM and facts['legacy_layout'] and not facts['mint_authority']
             and not facts['freeze_authority'])
    risky = owner_program != TOKEN_PROGRAM and (EXT_TRANSFER_HOOK in facts['extensions'] or facts['transfer_fee_bps'] > RISKY_FEE_BPS)
    return dict(out, in_scope=not (plain and source_is_pump), risky=risky, extensions=facts['extensions'],
                transfer_fee_bps=facts['transfer_fee_bps'], mint_authority=facts['mint_authority'],
                freeze_authority=facts['freeze_authority'])


def decide(outcome, scoped):
    """The entry consequence: a reject reason, or None (continue to the normal entry path)."""
    if outcome == BLOCKED:
        return REJECT_BLOCKED
    if outcome in (INCONCLUSIVE, NO_BUILD) and scoped.get('risky'):
        return REJECT_RISKY
    return None


def _excerpt(logs):
    """The failure-relevant tail of the logs: every 'failed' / error / frozen line plus the last lines, bounded."""
    if not isinstance(logs, list):
        return []
    lines = [str(line)[:MAX_LOG_CHARS] for line in logs]
    keep = [i for i, line in enumerate(lines) if ' failed' in line or 'rror' in line or 'frozen' in line.lower()]
    keep = sorted(set(keep[-MAX_LOG_LINES // 2:]) | set(range(max(0, len(lines) - MAX_LOG_LINES // 2), len(lines))))
    return [lines[i] for i in keep][-MAX_LOG_LINES:]


def _origin(logs):
    """``(program, parent, message)`` of the FIRST ``Program X failed`` line (the innermost failure: callers fail after it), or
    (None, None, None). ``parent`` is the program that invoked X (None at depth 1)."""
    stack = []
    for line in logs if isinstance(logs, list) else ():
        if not isinstance(line, str) or not line.startswith('Program '):
            continue
        parts = line.split(' ')
        if len(parts) >= 4 and parts[2] == 'invoke':
            stack.append(parts[1])
        elif len(parts) >= 3 and parts[2] == 'success':
            if stack:
                stack.pop()
        elif len(parts) >= 3 and parts[2] == 'failed:':
            program = parts[1]
            if stack and stack[-1] == program:            # the failing program is the top of the invoke stack
                stack.pop()
            return program, (stack[-1] if stack else None), line.split('failed:', 1)[1].strip()[:MAX_LOG_CHARS]
    return None, None, None


def _custom_code(detail, message):
    if isinstance(detail, dict) and type(detail.get('Custom')) is int:
        return detail['Custom']
    if isinstance(message, str) and 'custom program error: 0x' in message:
        try:
            return int(message.rsplit('0x', 1)[1].split()[0], 16)
        except (ValueError, IndexError):
            return None
    return None


def classify(err, logs, swap_index):
    """``(outcome, why)`` of one simulation result (``value.err`` / ``value.logs``). ``swap_index`` = the outer index of the swap
    instruction in the compiled message. RETRY = the payer side failed (fee, missing account, blockhash)."""
    if err is None:
        return OK, 'SIMULATED_SELL_SUCCEEDED'
    if isinstance(err, str) and err in RETRY_ERRORS:
        return RETRY, 'PAYER:' + err
    if isinstance(err, dict) and len(err) == 1 and next(iter(err)) in RETRY_ERRORS:
        return RETRY, 'PAYER:' + next(iter(err))
    if any('frozen' in str(line).lower() for line in (logs or ()) if isinstance(line, str) and line.startswith('Program log:')):
        return BLOCKED, 'ACCOUNT_FROZEN'
    if not (isinstance(err, dict) and isinstance(err.get('InstructionError'), list) and len(err['InstructionError']) == 2):
        return INCONCLUSIVE, 'SIM_ERROR:' + _short(err)
    index, detail = err['InstructionError']
    program, parent, message = _origin(logs)
    code = _custom_code(detail, message)
    if index != swap_index:
        return INCONCLUSIVE, 'NON_SWAP_INSTRUCTION_FAILED:%s:%s' % (index, _short(detail))
    if parent in TOKEN_PROGRAMS and program not in TOKEN_PROGRAMS:
        return BLOCKED, 'TRANSFER_HOOK_REJECTED:%s:%s' % (program, _short(detail))
    if program in TOKEN_PROGRAMS:
        if code == 17:
            return BLOCKED, 'ACCOUNT_FROZEN'
        if code == TOKEN_INSUFFICIENT_FUNDS:
            return RETRY, 'HOLDER_BALANCE_CHANGED'
        if code is not None and code not in BENIGN_TOKEN_CODES:
            return BLOCKED, 'TOKEN_PROGRAM_ERROR:%s:%d' % (program, code)
        return INCONCLUSIVE, 'TOKEN_PROGRAM_ERROR_NOT_TRANSFER_RULE:%s' % _short(detail)
    return INCONCLUSIVE, 'SWAP_FAILED:%s:%s' % (program, _short(detail))


def _short(value):
    try:
        return json.dumps(value, sort_keys=True, default=str)[:120]
    except (TypeError, ValueError):
        return type(value).__name__


def route_instructions(build):
    """The outer instructions of a Jupiter build, in transaction order, and the index of the swap one. The tip instruction is left
    out (a simulation needs no tip). Raises ValueError when the build has no swap instruction."""
    if not isinstance(build, dict) or not isinstance(build.get('swapInstruction'), dict):
        raise ValueError('NO_SWAP_INSTRUCTION')
    before = [*(build.get('computeBudgetInstructions') or []), *(build.get('setupInstructions') or [])]
    after = ([build['cleanupInstruction']] if build.get('cleanupInstruction') else []) + list(build.get('otherInstructions') or [])
    items = before + [build['swapInstruction']] + after
    if not 1 <= len(items) <= 64 or not all(isinstance(ix, dict) for ix in items):
        raise ValueError('INSTRUCTION_BUDGET')
    return items, len(before)


def compile_unsigned(build, payer):
    """``(transaction bytes, swap_index)``: a v0 message of the build's instructions with the lookup tables Jupiter returned,
    ``payer`` as the only signer, and ONE all-zero signature (nothing is signed). Raises ValueError on any malformed part."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    items, swap_index = route_instructions(build)
    instructions = []
    try:
        for ix in items:
            metas = []
            for account in ix.get('accounts') or []:
                if type(account.get('isSigner')) is not bool or type(account.get('isWritable')) is not bool:
                    raise ValueError('ACCOUNT_FLAGS')
                if account['isSigner'] and account['pubkey'] != payer:
                    raise ValueError('UNEXPECTED_SIGNER')
                metas.append(AccountMeta(Pubkey.from_string(account['pubkey']), account['isSigner'], account['isWritable']))
            instructions.append(Instruction(Pubkey.from_string(ix['programId']), base64.b64decode(ix.get('data') or '', validate=True), metas))
        tables = build.get('addressesByLookupTableAddress') or {}
        if not isinstance(tables, dict) or len(tables) > 8:
            raise ValueError('LOOKUP_TABLES')
        alts = [AddressLookupTableAccount(Pubkey.from_string(key), [Pubkey.from_string(a) for a in addresses])
                for key, addresses in tables.items()]
        message = MessageV0.try_compile(Pubkey.from_string(payer), instructions, alts, Hash.default())
    except ValueError:
        raise
    except Exception as error:                     # solders raises its own types for bad keys / compile failures
        raise ValueError('COMPILE_FAILED:' + type(error).__name__) from None
    if message.header.num_required_signatures != 1:
        raise ValueError('UNEXPECTED_SIGNER_COUNT')
    raw = bytes(VersionedTransaction.populate(message, [Signature.default()]))
    return raw, swap_index


def _token_account(account, mint):
    """``(owner, amount)`` of a token account of ``mint`` (legacy or Token-2022 layout), or None."""
    try:
        if not isinstance(account, dict) or account.get('owner') not in TOKEN_PROGRAMS:
            return None
        data = base64.b64decode(account['data'][0], validate=True)
        if len(data) < 165:
            return None
        from desk.security import base58
        if base58(data[:32]) != mint:
            return None
        return base58(data[32:64]), int.from_bytes(data[64:72], 'little')
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def _on_curve(key):
    from solders.pubkey import Pubkey
    try:
        return Pubkey.from_string(key).is_on_curve()
    except Exception:
        return False


def _burn_accounts(mint, program):
    try:
        from lean.features import burn_accounts
        return set(burn_accounts(mint, program))
    except Exception:
        return set()


def _creator(screen):
    try:
        from lean.features import dev_wallet
        return dev_wallet(screen)[0]
    except Exception:
        return None


# ------------------------------------------------------------------------------------------------- the filter
class HoneypotSim:
    def __init__(self, cfg, store, *, clock=time.time):
        self.cfg, self.store, self.clock = cfg, store, clock
        self.recent = deque()                     # times of the simulated candidates of the last hour (the hard cap)
        self.stats = {'skipped': 0, 'simulated': 0, 'capped': 0, 'rejected': 0, 'ok': 0, 'blocked': 0, 'inconclusive': 0,
                      'no_build': 0}
        from lean.sources import iter_rows
        since = self.clock() - 3600
        for row in iter_rows(store, 'observations', kind=KIND, since_ts=since):
            try:
                if json.loads(row['meta'] or '{}').get('simulated'):
                    self.recent.append(float(row['ts']))     # the cap survives a restart
            except (TypeError, ValueError):
                continue

    def health(self):
        return {'enabled': True, 'max_per_hour': self.cfg['max_per_hour'], 'last_hour': len(self.recent), **self.stats}

    # -- runner hook --------------------------------------------------------------------------------------------
    def reject(self, runner, candidate, cid, screen, gate):
        """Called by the runner after the pre-quote BUY gate and before any entry quote. True = rejected (the entry decision is
        recorded here); False = continue unchanged. Raises only for a store failure (the runner halts on it)."""
        mint = candidate.mint
        source = getattr(runner, '_source_of', {}).get(mint, DEFAULT_SOURCE)
        scoped = scope(screen, (screen.features or {}).get('token_program'), self._is_pump(runner, source))
        scoped['source'] = source
        if not scoped['in_scope']:
            self.stats['skipped'] += 1
            self._record(mint, cid, {'outcome': SKIPPED, 'why': 'PUMP_SPL_AUTHORITIES_REVOKED', 'scope': scoped, 'calls': 0,
                                     'simulated': False, 'decision': 'CONTINUE', 'attempts': []})
            return False
        full = self._full_tokens(screen, gate)
        now = self.clock()
        while self.recent and self.recent[0] < now - 3600:
            self.recent.popleft()
        calls = {'n': 0}
        if len(self.recent) >= self.cfg['max_per_hour']:
            self.stats['capped'] += 1
            outcome, why, attempts, simulated = INCONCLUSIVE, 'HOURLY_CAP', [], False
        else:
            self.recent.append(now)
            simulated = True
            try:
                outcome, why, attempts = self._simulate(runner.providers, mint, screen, full, calls)
            except (StoreError, sqlite3.Error):
                raise
            except Exception as error:                                  # isolation: never a crash, never a pass by accident
                outcome, why, attempts = INCONCLUSIVE, 'SIM_INTERNAL_ERROR:' + type(error).__name__, []
        reason = decide(outcome, scoped)
        self.stats[{OK: 'ok', BLOCKED: 'blocked', INCONCLUSIVE: 'inconclusive', NO_BUILD: 'no_build'}[outcome]] += 1
        self.stats['simulated'] += int(simulated)
        record = {'outcome': outcome, 'why': why, 'scope': scoped, 'paper_size_tokens': full, 'attempts': attempts,
                  'calls': calls['n'], 'simulated': simulated, 'decision': 'REJECT' if reason else 'CONTINUE', 'reject_reason': reason}
        ref, decision_ts = self._record(mint, cid, record)
        if reason is None:
            return False
        self.stats['rejected'] += 1
        runner.store.add_decision('entry', 'SKIP', mint=mint, candidate_id=cid, reasons=[reason], ts=self.clock(),
                                  features={'honeypot_sim': outcome, 'why': why, 'observation': ref, 'decision_ts': decision_ts,
                                            'signed': False, 'sent': False})
        note = getattr(runner, '_l07r_note', None)
        if callable(note):
            note(entry_reasons=[reason])
        return True

    @staticmethod
    def _is_pump(runner, source):
        if source == DEFAULT_SOURCE:
            return True
        extra = getattr(runner, 'extra_sources', None)
        try:
            from lean.sources import PumpGraduationSource
            return any(s.name == source and isinstance(s, PumpGraduationSource) for s in getattr(extra, 'sources', ()) or ())
        except Exception:
            return False

    @staticmethod
    def _full_tokens(screen, gate):
        """Our paper size in raw tokens: the constant-product output of the gate's size on the screened reserves (no fee: a slight
        over-estimate, the conservative side for a size-dependent check). None when unknown."""
        try:
            features = screen.features
            base, quote = int(features['base_reserve_raw']), int(features['quote_effective_raw'])
            from lean import adapters as A
            size = int(A.lamports(gate.size_sol))
            tokens = base * size // (quote + size)
            return tokens if tokens > 0 else None
        except Exception:
            return None

    def _record(self, mint, cid, record):
        decision_ts = self.clock()
        record = dict(record, kind='honeypot_sim_v1', mint=mint, decision_ts=decision_ts, signed=False, sent=False)
        raw = json.dumps(record, sort_keys=True, default=str).encode()
        meta = {'outcome': record['outcome'], 'decision': record['decision'], 'decision_ts': decision_ts,
                'simulated': record['simulated'], 'calls': record['calls']}
        return self.store.add_observation(KIND, raw, mint=mint, candidate_id=cid, meta=meta, ts=decision_ts), decision_ts

    # -- the simulation -----------------------------------------------------------------------------------------
    def _simulate(self, providers, mint, screen, full, calls):
        """``(outcome, why, attempts)``. Never raises a ProviderError: provider failures are INCONCLUSIVE."""
        holders, why = self._holders(providers, mint, screen, calls)
        if not holders:
            return INCONCLUSIVE, why, []
        attempts = []
        for holder in holders[:2]:                                  # one retry, with a different holder
            amount = min(holder['balance'], max(1, full // 10)) if full else max(1, holder['balance'] // 100)
            attempt = self._attempt(providers, mint, holder, amount, 'small', calls)
            attempts.append(attempt)
            if attempt['outcome'] != RETRY:
                break
        small = attempts[-1]
        outcome = INCONCLUSIVE if small['outcome'] == RETRY else small['outcome']
        why = small['why'] if small['outcome'] != RETRY or len(attempts) > 1 else small['why'] + ';NO_OTHER_HOLDER'
        if outcome == OK and self.cfg['full_size']:
            if full and small['balance'] >= full:
                big = self._attempt(providers, mint, dict(holder=small['holder'], token_account=small['token_account'],
                                                          balance=small['balance']), full, 'full', calls)
                attempts.append(big)
                if big['outcome'] == BLOCKED:
                    return BLOCKED, 'FULL_SIZE:' + big['why'], attempts
            else:
                attempts.append({'size': 'full', 'outcome': 'SKIPPED_BALANCE', 'amount': full, 'balance': small['balance']})
        return outcome, why, attempts

    def _holders(self, providers, mint, screen, calls):
        """Eligible holders, largest first: [{'holder', 'token_account', 'balance'}], or ([], why)."""
        features = screen.features or {}
        program = features.get('token_program')
        rows = None
        body = _raw_body(screen, 'holders')
        if body is not None:
            try:
                rows = json.loads(body)['result']['value']
            except (ValueError, KeyError, TypeError):
                rows = None
        if not isinstance(rows, list):
            try:
                calls['n'] += 1
                parsed, _raw, _meta = providers.helius.rpc('getTokenLargestAccounts', [mint, {'commitment': 'confirmed'}])
                rows = parsed['value']
            except ProviderError as error:
                return [], 'HOLDERS_UNAVAILABLE:' + error.code
            except (KeyError, TypeError):
                return [], 'HOLDERS_MALFORMED'
        exclude = _burn_accounts(mint, program) if program in TOKEN_PROGRAMS else set()
        if isinstance(features.get('pool_base_token_account'), str):
            exclude.add(features['pool_base_token_account'])
        picked = []
        try:
            for row in sorted((r for r in rows if isinstance(r, dict)), key=lambda r: -int(r['amount'])):
                if row['address'] not in exclude and int(row['amount']) > 0:
                    picked.append(str(row['address']))
        except (KeyError, TypeError, ValueError):
            return [], 'HOLDERS_MALFORMED'
        picked = picked[:MAX_HOLDER_ROWS]
        if not picked:
            return [], 'NO_ELIGIBLE_HOLDER'
        try:
            calls['n'] += 1
            result, _raw, _meta = providers.helius.get_multiple_accounts(picked)
        except ProviderError as error:
            return [], 'HOLDER_ACCOUNTS_UNAVAILABLE:' + error.code
        skip = set(BURN_OWNERS) | {features.get('pool'), _creator(screen)}
        holders = []
        for address, account in zip(picked, result['value']):
            parsed = _token_account(account, mint)
            if parsed is None:
                continue
            owner, balance = parsed
            if owner in skip or balance <= 0 or not _on_curve(owner):
                continue
            holders.append({'holder': owner, 'token_account': address, 'balance': balance})
        return holders, None if holders else 'NO_ELIGIBLE_HOLDER'

    def _attempt(self, providers, mint, holder, amount, size, calls):
        """One build + simulate for ``holder`` selling ``amount``. Returns the attempt record (outcome incl. RETRY)."""
        out = {'size': size, 'holder': holder['holder'], 'token_account': holder['token_account'], 'balance': holder['balance'],
               'amount': amount}
        from lean.route_check import UNSUPPORTED_ERROR_CODES, analyze_error
        try:
            calls['n'] += 1
            _quote, raw, _meta = providers.jupiter.quote(mint, SOL_MINT, amount, holder['holder'])
        except ProviderError as error:
            facts = analyze_error(error)
            if facts['error_code'] in UNSUPPORTED_ERROR_CODES:
                return dict(out, outcome=RETRY, why='BUILD_TAKER_UNFUNDED:' + facts['error_code'])
            if facts['conclusive'] or error.code == 'QUOTE_INVALID':
                return dict(out, outcome=NO_BUILD, why='BUILD_FAILED:' + facts['error_code'])
            return dict(out, outcome=INCONCLUSIVE, why='BUILD_UNAVAILABLE:' + facts['error_code'])
        try:
            from lean.providers import parse_json
            tx, swap_index = compile_unsigned(parse_json(raw), holder['holder'])
        except (ValueError, TypeError, UnicodeError, ArithmeticError, RecursionError) as error:
            return dict(out, outcome=NO_BUILD, why='BUILD_UNCOMPILABLE:' + str(error)[:80])
        if len(tx) > MAX_TX_BYTES:
            return dict(out, outcome=INCONCLUSIVE, why='TX_TOO_LARGE:%d' % len(tx))
        encoded = base64.b64encode(tx).decode()
        try:
            calls['n'] += 1
            # The ONLY simulateTransaction call site in lean/ (the no-sign guard checks this literal): unsigned, never broadcast.
            result, _raw, _meta = providers.helius.rpc('simulateTransaction', [encoded, {
                'encoding': 'base64', 'sigVerify': False, 'replaceRecentBlockhash': True, 'commitment': 'confirmed'}])
        except ProviderError as error:
            return dict(out, outcome=INCONCLUSIVE, why='SIM_UNAVAILABLE:' + error.code)
        value = result.get('value') if isinstance(result, dict) else None
        if not isinstance(value, dict) or 'err' not in value:
            return dict(out, outcome=INCONCLUSIVE, why='SIM_RESPONSE_INVALID')
        logs = value.get('logs') if isinstance(value.get('logs'), list) else []
        outcome, why = classify(value['err'], logs, swap_index)
        return dict(out, outcome=outcome, why=why, err=value['err'], units=value.get('unitsConsumed'), swap_index=swap_index,
                    logs_excerpt=_excerpt(logs))
