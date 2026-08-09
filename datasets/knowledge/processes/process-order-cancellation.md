---
document_id: process-order-cancellation
document_type: business_process
business_domains: [order]
environment: demo
version: "1.0"
owner: erp-platform
trust_level: trusted
access_scope: knowledge:erp:read
effective_from: 2026-07-01T00:00:00Z
source_type: authored
source_reference: null
---

# 订单取消流程

## 适用条件

用户明确表达取消订单的意图，并提供了 order_id。

## 处理步骤

1. **查询订单**：调用 `GET /orders/{order_id}` 获取订单当前状态。
2. **判断可取消性**：
   - status ∈ {CREATED, CONFIRMED} → 可无责取消。
   - status = SHIPPED → 可取消但需提示（可能产生物流拦截费用）。
   - status = DELIVERED → 不可取消，提示走退货流程。
   - status = CANCELLED → 不可取消，提示订单已取消。
3. **展示取消影响**：
   - CREATED/CONFIRMED："订单 #{order_id}（{product_name} × {quantity}）将被取消，金额 {amount} 元将退回。"
   - SHIPPED："订单 #{order_id} 已发货，取消可能产生物流拦截费用。确定取消吗？"
4. **获取用户确认**：等待用户确认。用户拒绝 → 流程终止。
5. **执行取消**：调用取消接口（当前模拟器为预留接口，需确认实际实现）。
6. **验证取消结果**：再次查询订单，确认 status=CANCELLED。

## 禁止行为

- **不得**取消 DELIVERED 状态的订单（应引导用户走退货流程）。
- **不得**重复取消 CANCELLED 状态的订单。
- **不得**在用户确认前执行取消操作。

## 审批要求

- 取消订单是 DANGEROUS 级别的写操作，需要审批。
- 审批前必须展示订单摘要和取消影响。

## 验证标准

- CREATED 订单取消 → status 变为 CANCELLED。
- DELIVERED 订单取消 → Agent 拒绝，提示走退货。
- 重复取消 → Agent 识别并拒绝。
