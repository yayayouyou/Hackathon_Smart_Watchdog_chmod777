"""跨行程檔案鎖：同一套語意，POSIX 走 flock，Windows 走 msvcrt。

`fcntl` 只存在於 POSIX。開發機是 macOS，展示機是 Windows，而擋在錢前面的
花費帳本（`realtime/ledger.py`）與外部快照的 observation chain
（`scrape/observations.py`）都靠這把鎖做跨行程互斥——少了它，動態版伺服器
連 import 都過不了（`ModuleNotFoundError: No module named 'fcntl'`）。

三件事刻意讓兩個平台一致，因為呼叫端是照這些性質寫的：

**非阻塞取鎖失敗一律拋 `BlockingIOError`。** POSIX 的 `flock(LOCK_NB)` 本來就拋
這個；Windows 的 `msvcrt.locking(LK_NBLCK)` 拋的是 `OSError(EACCES)`，不是它的
子類別。直接沿用會讓 `observations.observation_lock` 的 `except BlockingIOError`
漏接，把「別人正拿著鎖」這個預期中的情況變成未處理例外往上炸，而該處的正確
行為是回報 `RuntimeError: another external snapshot update holds ...`。

**鎖住的是檔案開頭第 1 個位元組，不是整個檔案。** Windows 鎖的是位元組區間，
所有參與者必須講好同一段；本模組是唯一上鎖的地方，所以講得好。區間可以超出
檔案結尾，因此鎖檔是空的也沒關係。POSIX 的 flock 本來就鎖整個 open file
description，`_REGION` 對它沒有意義。

**阻塞語意兩邊不完全相同，這點不假裝。** POSIX 的 `flock(LOCK_EX)` 無限等待，
維持原本行為一個位元都不動。Windows 沒有等價物（`LK_LOCK` 自己重試 10 秒就
放棄並改拋 `EDEADLOCK`），所以改成明確的重試迴圈，逾時拋 `TimeoutError`。

逾時而不是無限等待在這裡是安全的方向：鎖只在讀寫一份小 JSON 的數毫秒內被
持有，正常路徑碰不到；Windows 又會在行程結束時自動釋放鎖，所以持鎖者當掉
不會把鎖卡死。真的逾時就表示拿不到鎖，此時**寧可失敗**——花費帳本的預留寫在
送出請求之前，取不到鎖代表錢還沒花出去。
"""

from __future__ import annotations

import errno
import os
import time

#: 鎖住的區間長度：開頭 1 個位元組。見模組說明。
_REGION = 1

#: Windows 阻塞取鎖的預設上限（秒）。
DEFAULT_TIMEOUT = 30.0

#: Windows 重試間隔（秒）。掃描是人工發動的低頻動作，輪詢成本無關緊要。
_RETRY_INTERVAL = 0.05

if os.name == "nt":
    import msvcrt

    # MSDN `_locking`：LK_NBLCK 取不到鎖時 errno 是 EACCES；LK_LOCK 重試十次後
    # 才是 EDEADLOCK。兩個都當成「別人拿著」，其餘 errno 是真的出錯不能吞。
    _WOULD_BLOCK = frozenset({errno.EACCES, errno.EDEADLOCK})

    def _try_lock(fd: int) -> bool:
        """試一次。拿到回 True，被別人佔著回 False，其餘 errno 照原樣拋出。"""
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, _REGION)
        except OSError as exc:
            if exc.errno in _WOULD_BLOCK:
                return False
            raise
        return True

    def _lock_nb(fd: int) -> None:
        if not _try_lock(fd):
            raise BlockingIOError(errno.EWOULDBLOCK, "另一個行程正持有這把鎖")

    def _lock_blocking(fd: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"等不到檔案鎖（已等 {timeout:g} 秒）")
            time.sleep(_RETRY_INTERVAL)

    def _unlock(fd: int) -> None:
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _REGION)

else:
    import fcntl

    def _lock_nb(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _lock_blocking(fd: int, timeout: float) -> None:  # noqa: ARG001
        # flock 無限等待，沒有逾時可談；timeout 只為了兩邊同一個簽名而存在。
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _seek_to_region(handle) -> int:
    """把檔案位置歸零並回傳 fd。

    Windows 的位元組區間鎖從**目前檔案位置**起算，而 ``"a+"`` 開啟後的位置
    並未保證是 0。不歸零的話上鎖與解鎖可能落在不同區間，解鎖會失敗。
    """
    handle.seek(0)
    return handle.fileno()


def acquire(handle, *, blocking: bool = True,
            timeout: float = DEFAULT_TIMEOUT) -> None:
    """取得 ``handle`` 的獨占鎖。

    ``blocking=False`` 取不到鎖時拋 `BlockingIOError`（兩個平台一致）。
    ``blocking=True`` 時 POSIX 無限等待，Windows 等到 ``timeout`` 後拋
    `TimeoutError`。
    """
    fd = _seek_to_region(handle)
    if blocking:
        _lock_blocking(fd, timeout)
    else:
        _lock_nb(fd)


def release(handle) -> None:
    """釋放 ``acquire()`` 取得的鎖。"""
    _unlock(_seek_to_region(handle))
