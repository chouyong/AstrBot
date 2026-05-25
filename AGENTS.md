# AGENTS.md

## 交接记录（2026-05-21）

本文档用于记录当前阶段的业务架构设计、技术边界、仓库开发约束与后续待办，供电脑重启后继续工作使用。

当前仓库路径：

`D:\knowledgeBase\AstrBot`

当前阶段没有进行代码改动，主要完成的是业务架构设计与接入方案收敛。

---

## 一、当前已确认的业务目标

当前业务需要同时支持两类能力：

1. 买家和卖家之间的订单沟通 IM
2. 智能客服能力

补充要求：

- 业务前端是 `Vue2`
- 业务后端是 `PHP`
- 已经有一套可用的 `Java IM`，并且已经在别的 App 中使用
- 现在希望引入 `AstrBot`，但不希望它替代现有 IM 主链路

---

## 二、最终确认的总体架构

最终确认采用以下四层架构：

1. `Vue2 App`
   - 用户端、卖家端、客服工作台
   - 展示订单聊天页、客服页、AI 建议与摘要

2. `PHP 业务后端`
   - 订单、商品、用户、权限、客服工单
   - 作为业务中台
   - 也是 AI 编排层
   - 统一调用 Java IM 与 AstrBot

3. `Java IM`
   - 负责实时消息主链路
   - 负责会话、成员、文本、语音、图片、文件、三方会话、消息推送

4. `AstrBot`
   - 不负责 IM 主链路
   - 只负责智能能力
   - 包括智能客服、会话摘要、建议回复、风险识别、知识库问答

一句话总结：

`Java IM 负责消息主链路，AstrBot 负责智能能力，PHP 负责业务编排。`

---

## 三、明确的系统边界

### 1. Vue2 不直接调用 AstrBot

原因：

- 避免前端暴露 AI 接口和密钥
- 方便 PHP 做上下文组装、权限控制、限流、日志和审计

### 2. Java IM 不被 AstrBot 替代

原因：

- 已有成熟 IM 能力，没必要重复建设
- IM 的主链路稳定性比 AI 能力更重要
- 聊天不能被 AI 响应延迟影响

### 3. AstrBot 不进入主消息通道

不采用以下模式：

- 用户消息先发到 AstrBot
- 再由 AstrBot 转发到 IM

采用以下模式：

- 用户消息正常走 Java IM
- Java IM 的消息事件异步通知 PHP
- PHP 再异步调用 AstrBot
- AstrBot 返回摘要、建议回复、风险标签等旁路结果

---

## 四、订单聊天与智能客服的推荐形态

### 1. 买卖双方 IM

默认是订单维度的双方会话：

- 买家
- 卖家

### 2. 三方会话

当出现以下场景时升级为三方会话：

- 买家申请客服介入
- 卖家申请客服介入
- 退款、售后、纠纷
- 系统命中高风险规则

三方成员：

- 买家
- 卖家
- 平台客服

### 3. 智能客服的两种模式

#### 模式 A：独立智能客服页

适合：

- 售前 FAQ
- 平台规则咨询
- 发货、售后政策等常见问题

调用链：

`Vue2 -> PHP -> AstrBot -> PHP -> Vue2`

#### 模式 B：订单会话旁路辅助

适合：

- 卖家建议回复
- 客服介入前会话摘要
- 风险识别
- 客服标准答复建议

调用链：

`Java IM -> PHP webhook -> PHP -> AstrBot -> PHP -> Vue2 或 Java IM`

---

## 五、AstrBot 在本项目中的推荐职责

当前阶段建议 AstrBot 只承担以下能力：

1. 智能客服 FAQ 问答
2. 卖家建议回复
3. 客服介入前的会话摘要
4. 风险识别
   - 诈骗
   - 引流
   - 线下交易
   - 辱骂
   - 违规承诺
5. 语音转写后的理解与总结

不建议当前阶段让 AstrBot 做：

1. IM 主消息路由
2. 替代 Java IM 的会话系统
3. 默认自动插入所有订单会话公开发言
4. 自动仲裁

---

## 六、当前确认的接口分层

已确认接口边界分为四组：

### 1. Vue2 <-> PHP

负责：

- 订单聊天页元信息
- 客服申请介入
- AI 摘要展示
- AI 建议回复展示
- 智能客服页

### 2. Vue2 <-> Java IM

负责：

- 建立 IM 连接
- 收发消息
- 拉历史消息
- 已读未读
- 实时推送

### 3. PHP <-> Java IM

负责：

- 创建订单会话
- 添加客服成员
- 发送系统消息
- 发送机器人消息
- 获取最近消息
- 获取 IM token
- 接收消息 webhook

### 4. PHP <-> AstrBot

负责：

- 智能客服问答
- 会话摘要
- 建议回复
- 风险识别

---

## 七、当前确认的关键接口清单

以下为后续实现时优先参考的接口设计草案。

### A. Vue2 调 PHP

1. `GET /api/order-chat/detail?order_id=xxx`
2. `POST /api/order-chat/support/request`
3. `GET /api/order-chat/support/status?order_id=xxx`
4. `GET /api/order-chat/ai-summary?order_id=xxx`
5. `POST /api/order-chat/ai-reply-suggestion`
6. `POST /api/customer-service/chat`

### B. PHP 调 Java IM

1. `POST /internal/im/session/create`
2. `POST /internal/im/session/add-member`
3. `POST /internal/im/message/system`
4. `POST /internal/im/message/bot`
5. `GET /internal/im/messages/recent`
6. `POST /internal/im/auth/token`
7. `POST /api/im/webhook/message`（Java IM 回调 PHP）

### C. PHP 调 AstrBot

1. `POST /astrbot/customer-service/chat`
2. `POST /astrbot/order-chat/summary`
3. `POST /astrbot/order-chat/reply-suggestion`
4. `POST /astrbot/order-chat/risk-detect`

---

## 八、当前确认的表结构方向

PHP 侧建议优先准备以下业务表：

1. `order_chat_session`
2. `order_chat_support_ticket`
3. `order_chat_ai_summary`
4. `order_chat_ai_reply_suggestion`
5. `order_chat_ai_risk_event`
6. `customer_service_ai_session`
7. `customer_service_ai_message`

这些表的详细字段此前已经在对话中完成第一版设计，后续如继续实现，应先落：

- 订单会话主表
- 客服申请表
- AI 摘要表

---

## 九、建议的第一期落地范围

当前建议的第一期最小可上线版本：

1. 使用 Java IM 承担买卖双方聊天
2. PHP 建立订单与 IM 会话映射
3. 支持客服申请介入
4. PHP 调 Java IM 把客服加入三方会话
5. Java IM 把消息事件 webhook 给 PHP
6. PHP 调 AstrBot 生成：
   - 客服摘要
   - 卖家建议回复
   - 风险识别
7. Vue2 展示：
   - 聊天页
   - AI 建议回复
   - 客服摘要

当前不建议第一期实现：

1. 机器人自动插话所有订单会话
2. 多机器人复杂编排
3. 自动仲裁
4. 用 AstrBot 替代 Java IM

---

## 十、推荐的开发顺序

重启后建议按以下顺序继续：

1. 先画时序图
2. 细化 PHP 服务层设计
3. 固化 Java IM webhook 事件格式
4. 确认 Vue2 页面模块拆分
5. 再做 AstrBot 调用层协议

更具体的顺序建议：

1. PHP 打通订单会话与 Java IM 会话映射
2. Vue2 接 Java IM 完成订单聊天页
3. PHP 实现客服介入流程
4. Java IM webhook 通知 PHP
5. PHP 接 AstrBot 做摘要和建议回复
6. Vue2 展示 AI 结果

---

## 十一、下次继续时应优先完成的内容

下次继续时，建议优先输出以下两类文档或设计：

1. `时序图`
   - 买卖双方消息流
   - 客服介入流
   - 智能客服问答流
   - Java IM webhook -> PHP -> AstrBot 旁路流
2. `PHP 服务层设计`
   - `OrderChatService`
   - `SupportService`
   - `ImGatewayService`
   - `AiOrchestratorService`
   - `AiResultService`

如果还要继续细化，可再补：

3. Vue2 页面模块拆分
4. Java IM webhook 到 AstrBot 的处理伪代码

---

## 十二、当前仓库开发约束（中文整理）

以下是当前仓库已有开发约束，后续继续工作时需遵守。

### 启动方式

#### Core

```bash
uv sync
uv run main.py
```

默认 API / WebUI 地址：

`http://localhost:6185`

#### Dashboard（WebUI）

```bash
cd dashboard
pnpm install
pnpm dev
```

默认地址：

`http://localhost:3000`

### 代码规范与提交要求

1. 修改 WebUI 时要保持组件化，避免重复代码
2. 不要新增类似 `xxx_SUMMARY.md` 的报告文件
3. 完成后运行：

```bash
ruff format .
ruff check .
```

4. 提交信息使用 Conventional Commits，例如：
   - `feat: add intelligent customer service integration`
   - `fix: resolve order chat permission issue`
5. 所有新注释使用英文
6. 路径处理使用 `pathlib.Path`
7. 使用 AstrBot 提供的路径工具获取数据目录和临时目录

### pre-commit

如需启用：

```bash
pip install pre-commit
pre-commit install
```

---

## 十三、重启后继续工作的建议提示词

重启后如果要快速续上，可以直接告诉下一位助手：

`请基于 AGENTS.md 继续，先输出 Vue2 + PHP + Java IM + AstrBot 的时序图和 PHP 服务层设计。`

或者：

`请基于 AGENTS.md 继续，把订单聊天、客服介入、智能客服三条链路画成可开发的接口与时序图。`

---

## 十四、备注

本次主要完成的是架构设计与交接整理，没有对仓库代码做业务实现修改。

如果后续开始编码，建议先在当前仓库里新增一份更贴近实现的设计文档或直接落到对应模块代码中，不要重复讨论“是否替换 Java IM”这个问题。该问题已在本次结论中明确：

`不替换 Java IM；AstrBot 仅作为智能能力层引入。`
