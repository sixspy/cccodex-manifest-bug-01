# 规则轨单现行修复说明（manifest-bug-01）

## 病灶

旧版把"现行轨凭证"同时放在 `meta` 表两个键里：`rail_current=rail_a`、
`rail_current_alt=rail_b`，两种凭证并存（双开现行）。列表面读 `rail_current`，
登记面读 `rail_current_alt`，于是灌库门槛松紧乱跳、清洗入库结果漂。

## 选边（全局只留一种凭证）

**采用：旁路表一次换完现行轨。** 未采用"晋升不可变轨文件"方案，两种凭证不并存。

- `rail_pointer`：单行指针表（`CHECK (id = 1)` 强制只有一行），是现行轨的唯一凭证；
  切换即在一个事务里整行换完。
- `rule_rails`：规则轨目录，轨号 ↔ 规则版本 ↔ 灌库门槛（最小字节数）同源，
  门槛只从这里读，保证"轨号与门槛必须一致"。
- `rail_switch_log`：切换登记。成功、失败都留痕（含失败因、操作人），
  规则队列与清洗回放台都能读现行轨号与失败因。
- 旧 `meta` 双键在 `init_db` 迁移中一律作废删除；旧库以 `rail_current` 为现行轨登记，
  迁移动作本身也写进 `rail_switch_log`（"双轨凭证合并…"）。
- 读轨入口只有一个：`app/services/rails.py::current_rail_in`，
  批次卡片、对象详情、规则队列、清洗回放台四处全部经它读轨。
- 双开即停：指针不是唯一一行（或指向不存在的轨）时，所有读写面直接 409 报错停住。

## 排他

两侧值班同时点切换不会双写：进程内 `threading.Lock` 串行化 +
库级 `BEGIN IMMEDIATE`（`busy_timeout=5000`）兜底，改指针与写登记同事务提交。
一致性不变式：成功登记链严格首尾相接（下一条 `from_rail` == 上一条 `to_rail`），
测试 `test_concurrent_switches_never_double_write` 直接断言该链。

## 改动面

| 面 | 位置 |
|----|------|
| 批次卡片 | `GET /api/batches` 带 `rail`；列表每行显示现行轨 |
| 对象详情 | `GET /api/batches/{id}` 带 `rail`；登记按现行轨门槛校验 |
| 规则队列 | `GET /api/rails`（现行/全部轨/切换登记）；`POST /api/rails/switch` |
| 清洗回放台 | `GET /api/close-runs` 带 `rail`；每次闭合留有所跑轨号、版本、门槛 |

## 复现 / 二次验收步骤

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # 15 项全过（含并发排他、双开即停、旧库迁移）
.venv/bin/python run.py                # http://127.0.0.1:6900/login · admin / 123456
```

页面验收（四处同查）：

1. 登录后顶栏常驻"现行轨：rail_a · v1 · 门槛 ≥0 字节（全局唯一）"。
2. **落地批次**（批次卡片）、**批次详情**（对象详情）、**规则队列**、**清洗回放台**
   四页显示的现行轨号一致。
3. 规则队列点"切到此轨"→ rail_b：四页现行轨同时变 `rail_b · v2 · 门槛 ≥100 字节`；
   切换登记表新增一条成功记录。
4. 再点一次切到 rail_b → 报"已是现行轨"；切 `rail_z` → 报"目标轨 rail_z 不存在"；
   两条失败都带失败因留在登记表。
5. 对象详情登记 10 字节对象 → 报"未过门槛：现行轨 rail_b（v2）要求字节数 ≥ 100"；
   登记 150 字节成功；闭合清单后，清洗回放台该批次记录轨号为 rail_b/v2/≥100，
   与回放台顶部现行轨读数对得上。

本机库直查（页面之外复核）：

```bash
sqlite3 instance/manifest_lab.db \
  "SELECT * FROM rail_pointer; SELECT rail_code,rule_version,min_byte_size FROM rule_rails;
   SELECT ok,from_rail,to_rail,reason,operator FROM rail_switch_log ORDER BY id DESC LIMIT 5;
   SELECT batch_id,rail_code,rule_version,min_byte_size FROM close_runs;"
-- rail_pointer 必须只有一行；meta 表不得再有 rail_current / rail_current_alt
```

并发复核（排他）：

```bash
.venv/bin/python -m pytest tests/test_rail_single_current.py::test_concurrent_switches_never_double_write -q
```
