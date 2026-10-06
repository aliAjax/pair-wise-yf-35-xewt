# 反兴奋剂检测与结果管理

只使用 Python 标准库和 SQLite 的模块化项目，默认端口 `8301`。业务规则集中在 `src/rules.py`，用例编排（含离线采集、对账、回填、幂等与恢复）在 `src/service.py`，`app.py` 只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、跨对象校验（封条交接、B 样门禁等）。
- `src/repository.py`：SQLite 建表、查询、事务和乐观锁。
- `src/service.py`：离线采集包、实验室结果上报、对账合并、回填、人工判读、通知与幂等恢复。
- `src/http_api.py`：HTTP 路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `tests/`：完整链路、结果更正/续办/恢复、规则失败场景和 HTTP 端到端测试。

## 业务链路

```
运动员 → 样本(现场采集) → 封条交接(承运/签收) → 实验室结果(A样/B样, 可多修订)
      → 对账按 封条号+采样时刻 合并 → 案件（自动立案/临时禁赛/听证/结案）
```

核心规则：

- **赛外检查无信号**：`collection-packets` 先在本地保存采集与封条信息，网络恢复后整包补传；样本与封条用业务自然键生成确定性 ID，重复补传/服务重启不产生重复记录。
- **结果可更正**：实验室结果按 `report_no + revision` 版本化，复检更正会把同一样本同一等分（A/B）的旧修订标记为 `superseded`。
- **结果一更新，临时禁赛重新确认**：对账到新修订时，处于停赛或听证准备中的案件回到 `suspended` 并重新确认；已结案/申诉中的案件收到新结论不静默改判，转人工。
- **B 样确认前不能结案**：`decide=sanction` 必须存在已匹配的 B 样阳性结果；B 样阴性则解除临时禁赛、案件退回重开。
- **对账断点续办**：只处理 `recorded` 以及"已匹配但案件链尚未消化"的结果；中断后重跑只办没对完的样本。
- **重启不重复**：自动立案、通知、人工队列全部使用确定性 ID，并以案件上的 `last_result_id` 记账，重放不重复立案或通知。
- **旧记录回填**：样本缺封条号时，按运动员 + 采样时刻唯一命中封条交接记录后回填（状态不变）；对账时也会顺带回填；查不到或命中多个则进入 `manual_review` 人工队列。
- **判不出交人工**：实验室结果无法唯一定位样本时挂起为人工判读，人工确认归属后沿正常链路继续推进。

## 对象与状态

- `athlete`：active → retired。
- `sample`：scheduled → collected → sealed → in_transit → received → analyzed → adverse/cleared/atypical；对账 `reconcile_findings` 会按最新结论改写结论态；`backfill_seal` 原地回填封条号。
- `seal_handover`：recorded → handed_over → received（封条交接链，发货前必须已交接）。
- `lab_result`：recorded → matched → superseded；无法匹配时 recorded → manual_review → matched/dismissed。
- `case`：open → suspended → hearing → closed → appeal → closed；更正可 `reconfirm_suspension`（回 suspended）或 `lift_suspension`（到 reopened）。
- `notification`：确定性 ID 的已发通知记录。
- `manual_review`：pending → resolved。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用 `?status=` 过滤（支持 athletes/samples/seal_handovers/lab_results/cases/notifications/manual_reviews）。
- `POST /api/<kind>`：创建对象，请求体为 JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交 `{"action":"动作名","data":{...},"expected_version":数字}`。
- `POST /api/collection-packets`：离线采集包补传（幂等）。
- `POST /api/lab-results/ingest`：实验室结果上报（首报/复检更正）。
- `POST /api/reconcile`：执行对账合并（可传 `{"result_ids":[...]}` 限定范围）。
- `POST /api/backfills/seals`：批量回填旧记录封条号。
- `POST /api/manual-reviews/<id>/resolve`：人工判读 `{"resolution":"matched|rejected","sample_id":...}`。
- `GET /api/audit`：读取审计记录。

身份通过 `X-User-Id` 和 `X-Role` 请求头传入（viewer/admin/inspector/lab/panel；系统自动动作记为 system）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
