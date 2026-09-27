<!--
harness framing（这一段是我写的，不是外部原文）：
本目录是 PyCQA/isort 在 commit fd8bd07 的完整检出。下面「外部原文」是
https://github.com/PyCQA/isort/issues/1815 的正文，逐字粘贴，未做改写。
运行测试用：~/venvs/isort/bin/python -m pytest
-->

规则（我加的，不是 issue 原文）：不要修改 tests/ 下既有测试文件，新增文件可以。
判据由第三方持有的测试副本决定，改既有测试不会让它变绿。

# 任务：修掉下面这个 issue 描述的问题

外部原文（来源 https://github.com/PyCQA/isort/issues/1815 ，标题 "Tuple sorting doesn't consider profile"，报告人 kasium，2021-09-24 提出，2026-07-12 关闭）：

---

The tuple, list, etc sorting options don't consider the profile option.

Code:
```python
# isort: tuple
data = ("therearesuperlong", "therearesuperlong", "therearesuperlong", "therearesuperlong", "therearesuperlong")
```

`isort dummy.py --profile black`
```python
# isort: tuple
data = ('therearesuperlong', 'therearesuperlong', 'therearesuperlong', 'therearesuperlong',
 'therearesuperlong')
```

`black dummy.py`
```python
# isort: tuple
data = (
    "therearesuperlong",
    "therearesuperlong",
    "therearesuperlong",
    "therearesuperlong",
    "therearesuperlong",
)
```

I would expect that these sorting options consider the profile as normal. Also, the sorting options are not documented but noted here #1358
