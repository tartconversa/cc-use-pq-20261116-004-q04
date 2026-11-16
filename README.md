# 炭素材料证据与产能台账

本地 FastAPI 服务记录生产批次、实验室检验、发运、产线产能、客户订单与交付证据。所有示例记录均为合成数据，不连接真实工厂。

## 运行

在已准备好锁定依赖的 Python 环境中执行 uvicorn carbon_trace.api:app，通过 CARBON_TRACE_DB 指定 SQLite 文件。接口说明见 /docs。执行 python -m carbon_trace.cli demo --fixture fixtures/demo.json 可运行隔离的合成数据演示，输出日报、版本与对账结果。

## 数据约定

日期为 YYYY-MM-DD，事件时间必须带时区。工厂时区决定生产与发运所属日期，数量采用十进制吨。来源与外部事件编号组成不可变身份；完全相同的重试视为重复，不同内容的重试产生冲突拒绝。批次编号也具备幂等性，错误行不会回滚同批有效行。

登记工厂、产线和等级标准后提交生产批次。等级标准生效区间不得重叠；质量分析使用生产当日标准和该批次最新检验，区分未检验和检验不合格。准备发布时固定数据修订号与发布头；如其间新增事件或竞争版本，原候选失效，已发布数据保持不变。

订单预约只接受检验合格批次，且不得超过订单需求或批次余量。取消订单或释放预约后可重新使用余量。事故标记显示在日报；交付核收与物理发运量分别统计。

## 交付争议与结算

物理发运量来自发运事件，按其本地区发运日计入日报；此后出现的核收、复磅、破损、争议和结算都只形成交付证据，不会移动或改写历史发运吨数，已发布版本亦保持不变。

每次交付核收（POST /delivery/checks）是一条带证据编号的独立记录，可分别给出认可、破损、拒收、待确认吨数，四项必须等于核收吨数，并以 party 与 evidence_ref 标识核收方与证据来源。核收记录按业务核收时间与受理顺序确定当前证据，不按到达先后；结算之后才到达的核收标记 late，核收时间不晚于已记录证据的标记 out_of_order，二者都只进入复核提示，不会静默覆盖较新的有效结论。重复提交同一 check_id 且内容完全相同视为重放；内容不同则拒绝。复磅若覆盖先前核收，必须声明 supersedes_check_id 并附证据。

双方可围绕车次提出争议（POST /delivery/disputes），争议必须携带证据编号、主张吨数、类别和理由。争议可由 POST /delivery/disputes/{id}/resolve 解决或 POST /delivery/disputes/{id}/withdraw 撤回，需携带 expected_revision；相同内容的重复解决为重放，不同内容被拒绝。

结算（POST /delivery/settlements）是唯一能给出最终结论的动作，必须显式列出作为依据的核收记录 basis_check_ids 与争议 dispute_ids，并给出认可、破损、拒收、待确认四类数量，合计等于依据核收的实收吨数。结算不会自动采纳最新一条核收，也不会因新证据到达而改变；同一车次只能有一个生效结算，再次结算必须显式 supersedes_settlement_id，旧版本保留为 superseded 历史。已有生效结算后，除争议处理外不再接受新的核收；结算时仍有未解决争议时，结论标记为 provisional。

销售与订单视图区分：已承诺（commitment）、物理发运、实收、认可、破损、拒收、待确认、已结算、未结算与短少吨数，并以 settlement_status（no_commitments / pending / disputed / provisional / settled）与 current_basis 说明当前结算依据，unresolved_risks 列出未解决风险（缺核收、未结算、待确认、短少、破损、拒收、未决争议、结算后到达的证据等）。GET /orders/{order_id} 与 GET /delivery/orders/{order_id} 提供这些字段。

对账把物理发运与交付证据分开解释：GET /plants/{plant_id}/reconciliation 的 delivery 段给出 physical_shipment_total_tons 与 evidence_vs_physical_tons，GET /sources/{source}/reconciliation 的 delivery_evidence 段按来源解释发运与核收差异。

## 核心接口

使用 POST /plants、POST /lines、POST /specifications 和 POST /batches 建立数据。日报通过 GET /plants/{plant_id}/report 查询。先从 GET /status 取得修订号与发布头，再调用 POST /candidates 和 POST /candidates/{candidate_id}/publish。历史版本通过 GET /versions/{version} 查询。其他接口提供库存、质量、溯源、对账、产能预测、订单、预约、事故、交付核收、争议与结算。错误响应包含稳定代码与原因。
