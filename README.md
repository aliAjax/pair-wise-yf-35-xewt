# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。
- `handover`：封条交接记录，把样本与封条号、交接双方串成链。
- `lab_result`：实验室结果消息（A/B 样、阴阳性、`corrected_from` 复检更正），状态为 `pending → matched / manual`。

## 赛外检查对账链路

赛场无信号时，检查官先在本地完成采集与封条（`sample` 的 `collect` / `seal`），网络恢复后录入实验室结果并对账：

- `POST /api/lab_results`：录入一条实验室结果（可带 `seal_id`，或旧记录带 `athlete_id` + `collected_at`）。
- `POST /api/reconcile`：对账。按 `seal_id` 匹配样本；封条号缺失时按采样时刻 + 运动员回填匹配；匹配不上（零个或多个）进入 `manual` 人工队列。
- `POST /api/backfill_seals`：旧样本没有封条号时，按采样时刻 + 运动员从实验室结果回填封条号。
- `POST /api/lab_results/<id>/actions`，`{"action":"resolve","data":{"sample_id":...}}`：人工队列判不出时指定样本继续。

对账驱动案件链路，全部幂等、服务重启不重复立案 / 重复通知：

- 阳性结果落到样本（`record_result`），自动按结果立 `case` 并临时禁赛；同一结果只立一次案（幂等键 `lab-result:<id>:case`）。
- 复检更正：阳性→阴性则解除临时禁赛并结案（`no_sanction`）；阳性→阳性则重新确认禁赛（`reconfirm_suspension`）；已结案的新阳性则重开。
- B 样确认前不能结案：`case` 的 `decide` / `resolve_appeal` 在 `decision=sanction` 时要求样本 `b_confirmed=true`。
- 对账只处理 `pending` 结果；每一步（立案、通知）都按结果去重，中断后从断点续办。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
