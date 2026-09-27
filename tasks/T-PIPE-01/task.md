在这个目录里完成一个可验证的小数据处理流水线，全部用命令行完成，不要访问网络：

1. 生成 data/orders.csv：12 条数据行（不含表头），列为 order_id,date,region,amount。日期分布在 2026-01 到 2026-03，region 只用 east/west 两个值，amount 含小数。
2. 写 scripts/aggregate.py：读 data/orders.csv，按 region 统计订单数与金额合计（金额保留两位小数），输出 report/summary.json，格式 {"east":{"count":N,"sum":X.XX},"west":{...}}。
3. 写 tests/test_aggregate.py：用标准库 unittest，验证 summary.json 的两个 region 的 count 之和等于 12，且 sum 之和等于 csv 中 amount 的总和（允许 1e-6 误差）。
4. 运行 python3 scripts/aggregate.py 与 python3 -m unittest discover -s tests -v，确认全部通过。
5. 最后写 STATUS.md，内容必须包含一行 VERIFIED，以及一行严格形如 `UNITTEST_CASES=<数字>` 的用例数（等号左边就是这个 token，右边是整数）。
