<!--
harness framing（这一段是我写的，不是外部原文）：
本目录是 hynek/structlog 在 commit c6e7cad 的完整检出。下面「外部原文」是
https://github.com/hynek/structlog/issues/710 的正文，逐字粘贴，未做改写。
运行测试用：PYTHONPATH=src ~/venvs/structlog/bin/python -m pytest
（必须带 PYTHONPATH=src：本仓库是 src/ 布局，虚拟环境里装的是修复前的旧副本，
 不带它就会 import 到旧代码，你改的东西根本不会被测到。）
-->

规则（我加的，不是 issue 原文）：不要修改 tests/ 下既有测试文件，新增文件可以。
判据由第三方持有的测试副本决定，改既有测试不会让它变绿。

# 任务：修掉下面这个 issue 描述的问题

外部原文（来源 https://github.com/hynek/structlog/issues/710 ，标题 "Thread name CallsiteParameterAdder parameter doesn't work in async methods"，报告人 hynek（该仓库 owner），2025-03-08 提出，2026-05-10 关闭）：

---

This is based on @PrieJos's helpful analysis in https://github.com/hynek/structlog/issues/693#issuecomment-2629117069

In async log methods, callsite collection happens in the async logger thread which makes its thread-related information worthless:

```python
# /// script
# dependencies = [
#   "structlog",
# ]
# ///

import asyncio
import logging
import sys

import structlog

sl = structlog.get_logger()
logger = logging.getLogger()

logging.basicConfig(
    stream=sys.stdout,
    format=(
        "%(process)d %(processName)s %(module)s"
        " %(funcName)s:%(lineno)d %(threadName)s %(taskName)s"
        " %(message)s"
    ),
    datefmt=r"%Y-%m-%dT%H:%M:%S",
    level=logging.INFO,
    encoding="utf-8",
)

structlog.configure(
    processors=[
        structlog.processors.CallsiteParameterAdder(
            [
                structlog.processors.CallsiteParameter.PROCESS,
                structlog.processors.CallsiteParameter.PROCESS_NAME,
                structlog.processors.CallsiteParameter.MODULE,
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
                structlog.processors.CallsiteParameter.THREAD_NAME,
            ]
        ),
        structlog.processors.LogfmtRenderer(),
    ],
)


async def afunc():
    await sl.ainfo("async")
    logger.info("stdlib async")

def func():
    sl.info("sync")
    logger.info("stdlib sync")


asyncio.run(afunc())
func()
```

Notably, thread name is wrong and so would be a task name as shown in #698:

```
event=async process=55347 process_name=n/a module=t filename=t.py func_name=afunc lineno=47 thread_name=asyncio_0
55347 MainProcess t afunc:48 MainThread Task-1 stdlib async
event=sync process=55347 process_name=n/a module=t filename=t.py func_name=func lineno=51 thread_name=MainThread
55347 MainProcess t func:52 MainThread None stdlib sync
```

Not sure how to deal with this most elegantly – I feel like we'll have to pass thread/task objects into the event_dict or something?

I _think_ the `process_name` is in fact correct, since `logging` just [sets it unconditionally](https://github.com/python/cpython/blob/72e5b25efb580fb1f0fdfade516be90d90822164/Lib/logging/__init__.py#L359-L371) and we get `n/a` in sync code too. Happy to be corrected, though.
