"""跨平台檔案鎖：兩個平台必須有同一套可觀察行為。

這些測試存在的理由是一個真實的失敗：`fcntl` 只存在於 POSIX，Windows 上
`realtime/ledger.py` 連 import 都過不了，動態版伺服器與 53 條測試一起倒。
修完之後，要鎖住的是「兩邊語意一致」這件事本身——特別是非阻塞取鎖失敗
必須拋 `BlockingIOError`，因為 `observations.observation_lock` 是照這個
契約寫的，Windows 原生拋的 `OSError(EACCES)` 不是它的子類別，會漏接。
"""

from __future__ import annotations

import pathlib
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog import filelock


def test_acquire_then_release_is_reusable(tmp_path):
    p = tmp_path / "a.lock"
    for _ in range(3):
        with p.open("a+") as fh:
            filelock.acquire(fh)
            filelock.release(fh)


def test_non_blocking_refusal_is_blockingioerror(tmp_path):
    """兩個平台都必須是 BlockingIOError，不是各自的原生例外。

    Windows 的 `msvcrt.locking(LK_NBLCK)` 原生拋 `OSError(EACCES)`，
    POSIX 的 `flock(LOCK_NB)` 拋 `BlockingIOError`。呼叫端只接後者，
    所以正規化這件事必須被測試綁住。
    """
    p = tmp_path / "b.lock"
    with p.open("a+") as holder:
        filelock.acquire(holder)
        try:
            with p.open("a+") as other, pytest.raises(BlockingIOError):
                filelock.acquire(other, blocking=False)
        finally:
            filelock.release(holder)


def test_lock_is_released_so_the_next_caller_gets_it(tmp_path):
    p = tmp_path / "c.lock"
    with p.open("a+") as holder:
        filelock.acquire(holder)
        filelock.release(holder)
    with p.open("a+") as other:
        filelock.acquire(other, blocking=False)   # 不該拋
        filelock.release(other)


def test_blocking_acquire_waits_for_the_holder(tmp_path):
    """阻塞取鎖要真的等，不是立刻失敗。"""
    p = tmp_path / "d.lock"
    held = threading.Event()
    done = threading.Event()

    def hold():
        with p.open("a+") as fh:
            filelock.acquire(fh)
            held.set()
            time.sleep(0.4)
            filelock.release(fh)
            done.set()

    t = threading.Thread(target=hold)
    t.start()
    held.wait(timeout=5)
    t0 = time.monotonic()
    with p.open("a+") as fh:
        filelock.acquire(fh, blocking=True, timeout=10)
        waited = time.monotonic() - t0
        filelock.release(fh)
    t.join(timeout=5)
    assert done.is_set()
    # 等到了才拿到；放寬下界避免在慢機器上因排程抖動誤判。
    assert waited > 0.1, f"阻塞取鎖只等了 {waited:.3f}s，看起來沒有真的等"


@pytest.mark.skipif(sys.platform != "win32",
                    reason="POSIX 的 flock 無限等待，沒有逾時可測")
def test_blocking_acquire_times_out_on_windows(tmp_path):
    """Windows 沒有無限等待的等價物，逾時要拋 TimeoutError 而不是默默成功。

    逾時在這裡是安全方向：花費帳本的預留寫在送出請求之前，取不到鎖代表錢
    還沒花出去。
    """
    p = tmp_path / "e.lock"
    with p.open("a+") as holder:
        filelock.acquire(holder)
        try:
            with p.open("a+") as other, pytest.raises(TimeoutError):
                filelock.acquire(other, blocking=True, timeout=0.3)
        finally:
            filelock.release(holder)
