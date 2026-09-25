"""Async SessionStore boundary for gateway event-loop safety."""

import ast
import asyncio
import threading
from pathlib import Path

import pytest

from gateway.session import AsyncSessionStore


class _SpyStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.label = "store"

    def read(self, value: str) -> str:
        self.calls.append((value, threading.get_ident()))
        return value


def _nearest_function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
    return None


def _is_awaited(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Await):
            return True
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
    return False


def test_gateway_async_code_uses_one_awaited_session_store_boundary() -> None:
    """Loop-side store calls must use the facade; raw store remains sync-only."""
    root = Path(__file__).resolve().parents[2]
    violations: list[str] = []
    for rel in ("gateway/run.py", "gateway/slash_commands.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for owner in (node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)):
            raw_aliases = {
                target.id
                for node in ast.walk(owner)
                if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in {"self", "_self"}
                and node.value.attr == "session_store"
                for target in node.targets
                if isinstance(target, ast.Name)
            }
            for node in ast.walk(owner):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if _nearest_function(node, parents) is not owner:
                    # A nested sync helper (for example run_sync) executes off-loop.
                    continue
                receiver = node.func.value
                if isinstance(receiver, ast.Name) and receiver.id in raw_aliases:
                    violations.append(
                        f"{rel}:{node.lineno} raw alias {receiver.id}.{node.func.attr}() in async {owner.name}"
                    )
                    continue
                if not (
                    isinstance(receiver, ast.Attribute)
                    and isinstance(receiver.value, ast.Name)
                    and receiver.value.id in {"self", "_self"}
                ):
                    continue
                if receiver.attr == "session_store":
                    violations.append(
                        f"{rel}:{node.lineno} raw session_store.{node.func.attr}() in async {owner.name}"
                    )
                elif receiver.attr == "async_session_store" and not _is_awaited(
                    node, parents
                ):
                    violations.append(
                        f"{rel}:{node.lineno} unawaited async_session_store.{node.func.attr}()"
                    )
    assert not violations, "\n".join(violations)


@pytest.fixture
def reset_store(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore

    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    store._db = None
    store._ensure_loaded()
    return store


@pytest.mark.asyncio
async def test_reset_finalizer_contended_lock_keeps_loop_alive_and_callback_on_loop(reset_store):
    store = reset_store
    facade = AsyncSessionStore(store)
    loop_thread = threading.get_ident()
    calls = []
    ticks = 0
    store._lock.acquire()

    def cleanup():
        assert store._lock.locked()
        calls.append(threading.get_ident())

    task = asyncio.create_task(facade.run_model_override_reset_cleanup_if_current('key', None, cleanup))
    try:
        for _ in range(5):
            await asyncio.sleep(0.01)
            ticks += 1
        assert ticks == 5 and not task.done()
    finally:
        store._lock.release()
    await asyncio.wait_for(task, 2)
    assert calls == [loop_thread]
    assert not store._lock.locked()


@pytest.mark.asyncio
async def test_cancelled_finalizer_never_runs_cleanup_later(reset_store):
    store = reset_store
    facade = AsyncSessionStore(store)
    calls = []
    store._lock.acquire()
    task = asyncio.create_task(facade.run_model_override_reset_cleanup_if_current('key', None, lambda: calls.append(True)))
    try:
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        store._lock.release()
    # Drain the worker pool as well: a cancelled generic to_thread callback
    # must not get a chance to mutate the conversation after cancellation.
    await asyncio.to_thread(lambda: None)
    await asyncio.sleep(0.05)
    assert calls == []
    assert store._lock.acquire(blocking=False)
    store._lock.release()


@pytest.mark.asyncio
async def test_finalizer_loads_off_loop_and_releases_lock_on_callback_error(reset_store, monkeypatch):
    store = reset_store
    store._loaded = False
    loop_thread = threading.get_ident()
    loads = []
    original = store._ensure_loaded_locked

    def load():
        loads.append(threading.get_ident())
        assert threading.get_ident() != loop_thread
        original()

    monkeypatch.setattr(store, '_ensure_loaded_locked', load)

    def fail():
        assert threading.get_ident() == loop_thread
        raise ValueError('cleanup failed')

    with pytest.raises(ValueError, match='cleanup failed'):
        await AsyncSessionStore(store).run_model_override_reset_cleanup_if_current('key', None, fail)
    assert loads
    assert store._lock.acquire(blocking=False)
    store._lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', ['selection', 'route', 'missing'])
async def test_finalizer_rechecks_ownership_after_contention(reset_store, replacement):
    from types import SimpleNamespace
    from gateway.session import SessionModelOverrideChangedError, SessionRouteChangedError
    store = reset_store
    store._entries['key'] = SimpleNamespace(session_id='old', model_override=None)
    calls = []
    store._lock.acquire()
    task = asyncio.create_task(AsyncSessionStore(store).run_model_override_reset_cleanup_if_current('key', 'old', lambda: calls.append(True)))
    try:
        await asyncio.sleep(0.02)
        if replacement == 'selection':
            store._entries['key'].model_override = {'model': 'new'}
        elif replacement == 'route':
            store._entries['key'] = SimpleNamespace(session_id='new', model_override=None)
        else:
            del store._entries['key']
    finally:
        store._lock.release()
    expected = SessionModelOverrideChangedError if replacement == 'selection' else SessionRouteChangedError
    with pytest.raises(expected):
        await task
    assert calls == []
    assert not store._lock.locked()


def test_candidate_session_module_origin():
    import gateway.run
    import gateway.session
    assert Path(gateway.session.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]
    assert Path(gateway.run.__file__).resolve().parents[1] == Path(__file__).resolve().parents[2]


def test_no_repository_local_claude_permissions_file() -> None:
    root = Path(__file__).resolve().parents[2]
    assert not (root / ".claude" / "settings.json").exists()
