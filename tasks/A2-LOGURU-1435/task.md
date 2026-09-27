<!--
harness framing（这一段是我写的，不是外部原文）：
本目录是 pypa/loguru 在 commit d33e85e 的完整检出。下面「外部原文」是
https://github.com/Delgan/loguru/issues/1435 的正文，逐字粘贴，未做改写。
运行测试用：~/venvs/loguru/bin/python -m pytest
-->

规则（我加的，不是 issue 原文）：不要修改 tests/ 下既有测试文件，新增文件可以。
判据由第三方持有的测试副本决定，改既有测试不会让它变绿。

# 任务：修掉下面这个 issue 描述的问题

外部原文（来源 https://github.com/Delgan/loguru/issues/1435 ，标题 "Crash while formatting exceptions when a traceback frame has tb_lineno=None (TypeError in linecache.getline())"，报告人 Jonas1312，2026-02-06 提出，2026-02-22 关闭）：

---

Hi

### Bug

I'm seeing Loguru crash “Logging error in Loguru Handler”) when formatting an exception whose traceback contains a frame with tb_lineno == None:

```
Date,Content
"2026-02-05T17:39:09.865Z","'--- End of logging error ---"
"2026-02-05T17:39:09.865Z","TypeError: '<=' not supported between instances of 'int' and 'NoneType'"
"2026-02-05T17:39:09.865Z","^^^^^^^^^^^^^^^^^^^^^^^^^"
"2026-02-05T17:39:09.865Z","if 1 <= lineno <= len(lines):"
"2026-02-05T17:39:09.865Z","File ""/usr/lib64/python3.12/linecache.py"", line 31, in getline"
"2026-02-05T17:39:09.865Z","^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
"2026-02-05T17:39:09.865Z","source = linecache.getline(filename, lineno).strip()"
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_better_exceptions.py"", line 211, in get_info"
"2026-02-05T17:39:09.865Z","^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^"
"2026-02-05T17:39:09.865Z","infos.append((get_info(tb.tb_frame, tb.tb_lineno), tb.tb_frame))"
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_better_exceptions.py"", line 239, in _extract_frames"
"2026-02-05T17:39:09.865Z","^^^^^^^^^^^^^^^^^^^^^"
"2026-02-05T17:39:09.865Z","frames, final_source = self._extract_frames("
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_better_exceptions.py"", line 454, in _format_exception"
"2026-02-05T17:39:09.865Z","yield from self._format_exception("
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_better_exceptions.py"", line 422, in _format_exception"
"2026-02-05T17:39:09.865Z","yield from self._format_exception(value, tb, is_first=True, from_decorator=from_decorator)"
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_better_exceptions.py"", line 573, in format_exception"
"2026-02-05T17:39:09.865Z","^^^^^^^^^^^^^^"
"2026-02-05T17:39:09.865Z","formatter_record[""exception""] = """.join(lines)"
"2026-02-05T17:39:09.865Z","File ""/app/.venv/lib64/python3.12/site-packages/loguru/_handler.py"", line 147, in emit"
"2026-02-05T17:39:09.865Z","Traceback (most recent call last):"
"2026-02-05T17:39:09.865Z","During handling of the above exception, another exception occurred:"

[Truncated]

"2026-02-05T17:39:08.861Z","Traceback (most recent call last):"
"2026-02-05T17:39:08.861Z","'--- Logging error in Loguru Handler #1 ---"

```

This ends up happening inside Loguru's exception formatter (loguru/_better_exceptions.py)  and bubbles via handler emit logic (loguru/_handler.py)



## Expected

Loguru should log the exception (even if it can't show a source line for frames with lineno=None).

## Actual

Loguru prints a "Logging error in Loguru Handler …" and raises: ```TypeError: '<=' not supported between instances of 'int' and 'NoneType'```

### Environment

- Python: 3.12.x (Linux)
- loguru==0.7.3
- anyio==4.12.1
- uvloop==0.22.1

```
loguru_logger.add(
            sink=sink_serializer,
            format="{message}",
            level=settings.log_level,
            filter=_filter,
            diagnose=False,
            backtrace=False,
        )
``` 

### Reproduction

I have tried to make an MRE without success, it's pretty hard to reproduce unfortunately.

## Notes

This appears to come from calling linecache.getline(filename, lineno) with lineno=None inside Loguru's exception formatter.

Frames with lineno=None are  known Python edge case on Python 3.12+ see https://github.com/python/cpython/issues/139531 https://github.com/python/cpython/issues/89726

I tried turning `diagnose` and `backtrace` on and off but it did not help.


## Suggested fix

In loguru/_better_exceptions.py, we could guard lineno before calling linecache.getline(): if lineno is None (or not an int),we skip the lookup and proceed with an empty/unknown source line?



Thanks
