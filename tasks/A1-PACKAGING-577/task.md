<!--
harness framing（这一段是我写的，不是外部原文）：
本目录是 pypa/packaging 在 commit b62954b 的完整检出。下面「外部原文」是
https://github.com/pypa/packaging/issues/577 的正文，逐字粘贴，未做改写。
运行测试用：PYTHONPATH=src ~/venvs/pkg/bin/python -m pytest
（必须带 PYTHONPATH=src：本仓库是 src/ 布局，虚拟环境里装的是修复前的旧副本，
 不带它就会 import 到旧代码，你改的东西根本不会被测到。）
-->

规则（我加的，不是 issue 原文）：不要修改 tests/ 下既有测试文件，新增文件可以。
判据由第三方持有的测试副本决定，改既有测试不会让它变绿。

# 任务：修掉下面这个 issue 描述的问题

外部原文（来源 https://github.com/pypa/packaging/issues/577 ，标题 "migrating from distlib.wheel: `packaging.utils.parse_wheel_filename` allows illegal platform tags"，报告人 asottile-sentry，2022-07-18 提出，2026-07-28 关闭）：

---

I'm looking to migrate a caller from utilizing `distlib.wheel` to instead use `packaging.utils.parse_wheel_filename` and my tests encountered an edge case which isn't handled by the `packaging` version

here's the distlib code: https://github.com/pypa/distlib/blob/91aa92e64ee5d5005901907eaa340a72f18d7212/distlib/wheel.py#L83

packaging's `parse_tag` doesn't validate the interpreter in any way -- whereas distlib required a particular structure

the test case in question: https://github.com/chriskuehl/dumb-pypi/blob/b3fc62c6519bdb84960d2b491b40ad065fb79a47/tests/main_test.py#L61

`playlyfe-0.1.1-2.7.6-none-any.whl` should not parse as a valid wheel, however `packaging` allows it (and parses as three separate tag-triplets: `2-none-any`, `7-none-any`, `6-none-any`)

I propose adding a little bit of validation to `parse_tag` to check that the tags are supported interpreter versions

I believe this involves something like:

```python
_INTERPRETER_RE = re.compile(fr'^(?:{"|".join(sorted(INTERPRETER_SHORT_NAMES.values()))}\d+')
```

and then utilizing that regex to validate the names (and throwing an appropriate exception) -- I think a new `InvalidTag` (extending `ValueError`) or somesuch exception would need to be created as well (similar to the ones in `packaging.utils` for the wheel / stdist filename)

wanted to propose this first before diving in and implementing it to save time -- thoughts?
