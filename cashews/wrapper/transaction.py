from __future__ import annotations

from contextvars import ContextVar, Token
from enum import Enum
from functools import wraps
from typing import TYPE_CHECKING

from cashews.backends.interface import Backend
from cashews.backends.transaction import LockTransactionBackend, TransactionBackend

from .wrapper import Wrapper

if TYPE_CHECKING:  # pragma: no cover
    from cashews._typing import DecoratedFunc

_transaction: ContextVar[Transaction | None] = ContextVar("transaction", default=None)


class TransactionMode(Enum):
    FAST = "fast"  # simple inmemory impl, allow to have lost updates,
    LOCKED = "locked"  # lock per key - not allow to work in parallel with the same key
    SERIALIZABLE = "serializable"  # global lock - not allow any parallel changes


class TransactionWrapper(Wrapper):
    transaction_mode = TransactionMode.LOCKED
    transaction_timeout = 10

    def set_transaction_timeout(self, timeout: int) -> None:
        self.transaction_timeout = timeout

    def set_transaction_mode(self, mode: TransactionMode) -> None:
        self.transaction_mode = mode

    def _get_backend(self, key: str) -> Backend:
        backend = super()._get_backend(key)
        tx: Transaction | None = _transaction.get()
        if tx:
            return tx.wrap(backend)
        return backend

    def transaction(
        self, mode: TransactionMode | None = None, timeout: float | None = None
    ) -> TransactionContextDecorator:
        mode = mode or self.transaction_mode
        timeout = timeout or self.transaction_timeout
        return TransactionContextDecorator(mode, timeout)


class TransactionContextDecorator:
    __slots__ = ["_mode", "_timeout", "_inner", "_return_token"]

    def __init__(self, mode: TransactionMode | None = None, timeout: float | None = None):
        self._mode = mode
        self._timeout = timeout
        self._inner = False
        self._return_token: Token | None = None

    @property
    def current_tx(self) -> Transaction | None:
        return _transaction.get()

    async def __aenter__(self) -> Transaction:
        if self.current_tx:
            self._inner = True
            return self.current_tx
        return self.start()

    def start(self) -> Transaction:
        tx = Transaction(self._mode, self._timeout)
        self._return_token = _transaction.set(tx)
        return tx

    def close(self):
        _transaction.reset(self._return_token)

    async def __aexit__(self, exc_type, exc_value, exc_tb) -> None:
        if not self.current_tx or self._inner:
            self._inner = False
            return
        if not exc_tb:
            await self.commit()
        else:
            await self.rollback()
        self.close()

    def __call__(self, func: DecoratedFunc) -> DecoratedFunc:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with self:
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    async def commit(self) -> None:
        if self.current_tx:
            await self.current_tx.commit()

    async def rollback(self) -> None:
        if self.current_tx:
            await self.current_tx.rollback()


class Transaction:
    __slots__ = ["_mode", "_timeout", "_backends"]

    def __init__(self, mode: TransactionMode | None = None, timeout: float | None = None):
        self._mode = mode
        self._timeout = timeout
        self._backends: dict[str, TransactionBackend] = {}

    def wrap(self, backend: Backend) -> Backend:
        if backend._id not in self._backends:
            self._backends[backend._id] = self._get_tx_backend(backend)
        return self._backends[backend._id]

    def _get_tx_backend(self, backend: Backend) -> TransactionBackend:
        if self._mode == TransactionMode.FAST:
            return TransactionBackend(backend)
        if self._mode == TransactionMode.SERIALIZABLE:
            return LockTransactionBackend(backend, serializable=True, timeout=self._timeout)
        return LockTransactionBackend(backend, serializable=False, timeout=self._timeout)

    async def commit(self) -> None:
        for tx_backend in list(self._backends.values()):
            await tx_backend.commit()

    async def rollback(self) -> None:
        for tx_backend in list(self._backends.values()):
            await tx_backend.rollback()
