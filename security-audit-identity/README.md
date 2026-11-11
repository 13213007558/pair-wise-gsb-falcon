# 审计中间件身份串号：根因与修复

鉴权中间件在 `process_request()` 中拒绝了请求，审计中间件的
`process_response()` 仍然执行；审计组件把身份存在**单例自身的实例属性**上，
并默认“自己的 `process_request()` 一定先跑过”，于是本次请求被记到了**上一次
请求的主体**名下。本目录在 Falcon（WSGI 与 ASGI，`independent_middleware`
两种取值）中还原该误用，并给出可直接运行的修复版。

## 一、根因

两个独立的错误假设叠加：

1. **中间件实例是跨请求共享的单例。** 每个中间件对象在应用整个生命周期内只
   构造一次，WSGI 的多线程、ASGI 的协程并发都会复用同一实例。把“本次请求的
   身份”写到 `self.current_identity` 上，本质是把请求级状态放到了全局共享存
   储，请求之间天然会互相覆盖。
2. **`process_request()` 与 `process_response()` 不保证成对执行。** Falcon 的
   调度链允许请求段（request leg）中途停止，而响应段（response leg）照常逆序
   执行。审计组件“我的 `process_request()` 一定先执行”的假设在短路场景下不
   成立，此时 `self.current_identity` 仍是上一次请求留下的值。

把缺失属性默认成 `''` 或 `None` 只是掩盖：它既不能区分“匿名请求”与“代码没
跑到”，还会把一次真实的鉴权拒绝记成一个名为空串的用户，审计证据仍然是伪造
的。正确做法是把身份绑定到**本次请求对象**，并用显式哨兵区分“身份未建立”。

## 二、Falcon 的调度语义（来自 `falcon/app.py` 与 `falcon/asgi/app.py`）

两个 flavor 的中间件调度逻辑同构（`__call__` / ASGI 对应方法）：

- 请求段按注册顺序执行 `process_request`；某组件抛出异常 → 进入错误处理，后续
  组件的 `process_request` 全部跳过；某组件设置 `resp.complete = True` → 循环
  `break`，同样跳过后续请求段组件，但**不进入异常处理**。
- 响应段统一逆序执行 `process_response(req, resp, resource, req_succeeded)`。
- `req_succeeded` 初始为 `False`，只有路由、`process_resource`、responder 全
  部无异常逃逸后才在同一 `try` 内被置为 `True`；任何 `process_response` 自身
  抛错又会把后续调用重新打回 `False`。

### `independent_middleware` 两种取值

| 取值 | 请求段异常短路 | `resp.complete` 干净短路 | 响应段行为 |
| --- | --- | --- | --- |
| `True`（默认） | 后续请求段跳过，**所有**响应段组件仍执行 | 后续请求段跳过，**所有**响应段组件仍执行 | 响应栈在装配时独立构建 |
| `False`（dependent） | 未成功跑完 `process_request` 的组件，其 `process_response` **不进响应栈** | 已执行过的组件照常进响应栈（异常未发生，入栈逻辑不受 `resp.complete` 影响） | 响应栈在请求段循环中边跑边入栈 |

关键推论：

- `independent_middleware=False` **不是**通用修复。它只让“异常型短路”跳过审
  计的响应段（代价是这次拒绝在审计里**彻底没有记录**，出现审计盲区）；对
  `resp.complete=True` 的干净短路完全无效——本目录的 `/soft-deny` 用例证明，
  两种取值下串号都会发生。
- 真正的修复必须让审计只信任本次请求建立的身份，与框架开关无关。

## 三、三个失败场景的实测调用顺序

中间件注册顺序：`AuthMiddleware` → `CleanShortCircuitMiddleware` → 审计。
身份按 `alice`、匿名、`soft`、`bob`、`alice`、`bad`、`alice` 交错发送
（见 `reproduce.py`）。

1. **请求短路（异常型，`/deny`）**：`auth.process_request` 抛
   `HTTPUnauthorized`。
   - `independent=True`：审计的 `process_request` 被跳过，`process_response`
     仍执行，读到上一次请求留下的 `alice`，`req_succeeded=False`。
   - `independent=False`：审计的 `process_response` 不执行，审计表没有这一行
     （拒绝事件丢失）。
2. **请求短路（干净型，`/soft-deny`）**：上游设置 `resp.complete=True`，无
   异常。两种取值下审计的 `process_request` 都被跳过而 `process_response`
   都执行，串号到 `alice`，且 `req_succeeded=True`——框架认为“没出异常”。
3. **资源阶段失败**：
   - `/boom` 抛自定义 `BusinessError`，被 `add_error_handler` 注册的处理器转
     换成正常的 `409` 响应；请求段都执行过，身份正确，但
     `req_succeeded=False`（异常确实发生过，是否被转换不影响该标志）。
   - `/fail` 抛未注册的 `RuntimeError`，走默认 `500`；同样
     `req_succeeded=False`。

WSGI 与 ASGI 的事件轨迹逐条一致（ASGI 仅方法名带 `_async` 后缀）。

## 四、`req_succeeded` 能说明什么、不能说明什么

**能说明**：在它为 `False` 时，路由/资源中间件/responder/响应中间件链路中有
异常逃逸（即使该异常随后被错误处理器转换成一个“好看”的 4xx 响应，如
`/boom` 的 409）。

**不能说明**：

- 不能说明鉴权是否通过。鉴权拒绝发生在 `process_request`，得到
  `req_succeeded=False`；但一个未认证请求若被干净短路（`resp.complete`），
  得到的是 `True`。
- 不能说明资源/responder 是否执行过。`/soft-deny` 的 responder 从未运行，
  `req_succeeded` 却是 `True`。
- 不能说明本次请求的身份是否已建立，更不能作为审计归因的依据。
- 不能说明最终 HTTP 状态码：业务代码主动 `resp.status = HTTP_403` 而不抛异
  常时，`req_succeeded` 仍为 `True`；被处理器转换的异常状态码是 409，标志却
  是 `False`。

## 五、修复方案（`secure_middleware.py`）

- 身份只写 `req.context`（Falcon 每请求新建的上下文），中间件实例上不保留任
  何可变请求状态。
- 模块级哨兵 `_NO_IDENTITY = object()` 显式表示“本请求身份未建立”；审计行用
  `identity_established: bool` + `attributed_identity` 两个字段，拒绝/匿名请
  求记为未建立而不是空字符串。
- `process_response()` 只读取本次请求上下文里的槽位，读不到就按未建立处理，
  因此无论框架如何短路、无论 `independent_middleware` 取何值，都不可能引用到
  另一次请求的主体。
- 鉴权组件只在认证成功后写 `req.context.identity`，失败即抛
  `HTTPUnauthorized`，不写任何身份。

## 六、运行方式

```bash
# 打印四种组合（WSGI/ASGI × independent True/False）的完整调用顺序与审计表
python reproduce.py

# 16 条断言：串号复现、干净短路在 dependent 模式下仍泄漏、修复版保证、
# req_succeeded 语义
python -m pytest test_audit_identity.py -v
```

文件清单：

- `vulnerable_middleware.py`：有缺陷的鉴权/审计组件与干净短路组件。
- `secure_middleware.py`：请求级身份绑定的安全审计中间件（可直接取用）。
- `app_factory.py`：WSGI/ASGI 应用、资源、自定义异常与错误处理器。
- `reproduce.py`：交错身份的端到端复现与汇总。
- `test_audit_identity.py`：pytest 断言。
