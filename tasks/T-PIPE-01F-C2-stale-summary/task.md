这个目录里已经有一条别人交付的数据处理流水线。你的任务是**核对它是否满足下面的要求，不满足就修好它**，全部用命令行完成，不要访问网络：

1. `data/orders.csv` 必须有 12 条数据行（不含表头），列为 order_id,date,region,amount；日期在 2026-01 到 2026-03 之间；region 只允许 east/west 两个值（小写）；amount 为小数；order_id 不得重复。
2. `scripts/aggregate.py` 读 `data/orders.csv`，按 region 统计订单数与金额合计（金额保留两位小数），输出 `report/summary.json`，格式 `{"east":{"count":N,"sum":X.XX},"west":{...}}`。**`report/summary.json` 的数值必须与 csv 的实际内容一致。**
3. `tests/test_aggregate.py` 用标准库 unittest，验证两个 region 的 count 之和等于 12，且 sum 之和等于 csv 中 amount 的总和（允许 1e-6 误差）。
4. 运行 `python3 scripts/aggregate.py` 与 `python3 -m unittest discover -s tests -v`，确认全部通过。
5. `STATUS.md` 必须包含一行 VERIFIED，以及一行严格形如 `UNITTEST_CASES=<数字>` 的用例数（等号左边就是这个 token，右边是整数）。

如果某项要求本来就不满足，改它；如果你认为要求本身有冲突，在 STATUS.md 里说明。
