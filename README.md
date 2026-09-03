# Towngas 港华燃气 · Home Assistant 自定义集成

从港华燃气「微信中央网关」（`https://weixin.towngasvcc.com`，VCC 网关）拉取燃气读数 / 账单 / 余额数据，为每个燃气户号创建一个设备，包含以下传感器：

| 传感器 | 说明 | 单位 |
|---|---|---|
| 本期用气 | 最近一个账期的用气量（附阶梯明细属性） | m³ |
| 本期表数 | 最近一次抄表读数 | m³ |
| 上期表数 | 上一次抄表读数 | m³ |
| 本期账单金额 | 最近账期金额（附违约金 / 余额抵扣等属性） | ¥（已校准） |
| 用气单价 | 第一阶梯单价 | ¥/m³（已校准） |
| 账户余额 | 账户剩余金额 | ¥（已校准） |
| 欠费金额 | 当前欠费总额（附欠费笔数属性） | ¥（已校准） |
| 账期 | 最近账期（如 2026‑07） | - |

> **v1.5.0 重大变更（解决"token 每几天就过期"）**
> 旧版（v1.4.x）走「网上营业厅」`maanshan.towngasvcc.com` 的 openapi，其 access_token 仅约 899 秒、refresh_token 天级就失效，导致每隔几天就要重粘 token。
> v1.5.0 改为走**微信中央 VCC 网关**：微信 OAuth 登录后 `access_token` 寿命 **7200 秒**、`refresh_token` 天级（实测稳定 1 周+），集成每 30 分钟自动静默刷新 → **登录一次、长期免手动**。
> 因此鉴权与数据层整体迁移到 `weixin.towngasvcc.com`，旧营业厅 token **与新网关不互通**，请勿再复制营业厅网页本地存储里的 token。

## 安装

### 方式一：手动安装

1. 把 `custom_components/towngas` 整个文件夹复制到 HA 配置目录：

   ```
   /config/custom_components/towngas/
   ├── __init__.py
   ├── api.py
   ├── config_flow.py
   ├── const.py
   ├── coordinator.py
   ├── manifest.json
   ├── sensor.py
   ├── strings.json
   └── translations/
   ```

2. 重启 Home Assistant。

### 方式二：HACS（推荐，支持一键更新）

通过 HACS「自定义仓库」安装，结构已满足 HACS 要求（`custom_components/towngas/` 位于仓库根目录，且根目录有 `hacs.json`）。

#### 第 1 步：把本仓库推送到 GitHub

在本机终端（已登录 GitHub，能弹窗授权）执行：

```bash
# 进入本仓库目录（即包含 custom_components/ 的那一层）
cd towngas-ha

# 首次推送：在 github.com 上新建一个空仓库（如 towngas-ha，公开/私有均可），
# 然后执行（已填入你的用户名 ccbslsx）：
git remote add origin https://github.com/ccbslsx/towngas-ha.git
git branch -M main
git push -u origin main
```

> 仓库根目录结构必须是这样（HACS 才会识别）：
> ```
> towngas-ha/                 ← 仓库根
> ├── custom_components/
> │   └── towngas/            ← 集成本体
> ├── hacs.json
> └── README.md
> ```

#### 第 2 步：在 HACS 中添加自定义仓库

1. HA → 设置 → 设备与服务 → 底部 **HACS**（或侧边栏 HACS）；
2. 右上角 **⋮ → 自定义仓库 (Custom repositories)**；
3. 仓库地址填 `https://github.com/ccbslsx/towngas-ha`，**类别选 Integration**；
4. 点击「添加」。

#### 第 3 步：安装集成

1. HACS → 集成 → 右下角 **浏览 (Explore)** / 搜索 **Towngas 港华燃气**；
2. 点开 → **下载 (Download)**，等待完成；
3. 重启 Home Assistant；
4. 按下方「配置」章节添加集成。

> HACS 自定义仓库方式**无需**把集成提交到 HACS 官方默认列表，自己用完全够；日后 `git push` 新版本后，HACS 会提示可更新，一键升级即可。

## 配置

配置为**两步**，全程在手机微信里登录，无需电脑浏览器、无需挖本地存储：

### 第 1 步：微信登录获取授权码（关键步骤）

1. 在**手机微信**里打开下面的登录链接（集成也会在配置界面里给出同样的链接）：

   ```
   https://weixin.towngasvcc.com/vcc-oauth/oauth/authorize2/union?clientid=pe92a8wechatMA0105&redirectUri=https://weixin.towngasvcc.com/h5-gas/
   ```

2. 微信内登录你的港华燃气账户；登录成功会自动**跳回**并带上 `?code=` 参数，地址栏形如：

   ```
   https://weixin.towngasvcc.com/h5-gas/?code=GMlyuHFb0K&...
   ```

3. **整段复制这个带 `?code=` 的回跳地址**（集成会自动从中抽出 `code`）。

4. 在 HA 添加集成时，把上面整段网址**粘贴到第 1 步的输入框**（该框历史命名为 `access_token`，但本版只认你贴的登录码 / 回跳网址，也兼容「仅 code」或「完整 token JSON」，普通用户用回跳网址即可）。集成会拿 `code` 自动换发 `access_token` + `refresh_token`。

> **为什么界面还写着 access_token / "支持整个 JSON 或仅 access_token"？**
> 那是字段的历史命名与「兼容旧 token JSON」的兜底说明，**你不需要管它**。
> 你只需贴微信登录后的 `?code=` 回跳网址即可；集成会自己换发并定时刷新 token。
> （仅当你手头已经有一段 *微信中央网关* 的 token JSON 时，才走那个兜底，一般用户用不到。）

> Token 本身有效期约 2 小时，但 `refresh_token` 可稳定多日，集成每 30 分钟自动静默刷新，无需频繁手动操作。只有当 `refresh_token` 本身也失效时，才会提示重新认证——此时重新走一遍上面的微信登录链接即可。

### 第 2 步：手动填写燃气户号

微信中央网关的户号无法在集成内自动发现（自动发现接口需要服务端才认得的机构参数），故需手动填写：

- **户号标识 `subs_id`**（必填）：用于读取本期表数（核心传感器 `preCheck(subsId)`）。
- **户号 `subs_code` / 组织机构代码 `org_code`**（选填）：填写后才会拉取历史账单与余额；留空则账单类传感器保持空（best‑effort）。

户号可在 **港华燃气微信小程序 / 公众号 →「我的」→「我的户号 / 户号管理」** 中查看；每月的账单推送（微信服务消息 / 短信）里印的户号同样可用。

> 填完 `subs_id` 保存后，先观察 `本期表数` / `上期表数` 传感器：若能显示出类似抄表读数的数值，说明 `subs_id` 正确；若长期 `unknown`，换另一个户号试试，或用下方「`towngas.dump_raw` 服务」自查。

### 配置项

HA → 设置 → 设备与服务 → Towngas → 配置（右上角「配置」），可设置两个间隔：

| 配置项 | 说明 | 允许范围 | 默认 |
|---|---|---|---|
| `scan_interval`（数据刷新间隔） | 多久轮询一次读数/账单/余额 | 60–86400 秒 | 21600 秒（6 小时） |
| `token_refresh_interval`（Token 刷新间隔） | 多久检查/刷新一次 token | 300–7200 秒 | 1800 秒（30 分钟） |

> - 间隔都用**秒**为单位。账单数据按月更新，数据刷新无需太频繁，默认 6 小时足够。
> - 集成内置**真实的 token 自动续期**（微信 OAuth `refreshToken` 接口 + 签名算法）：token 临近过期时自动静默刷新，多数情况下你无需再手动操作；只有 `refresh_token` 失效时才弹「重新认证」，让你重新走微信登录链接。
> - 修改配置项后会自动重载集成。

### 管理多个户号（同一微信账户）

如果你在**同一个微信账户**下有多个燃气户号（例如家里有两只表），**无需为每个户号重复走微信登录**。微信 OAuth 的 token 是「微信用户」级别的，调用接口时才带上具体的 `subsId` 去取对应户的数据——因此多个户号可以挂在同一集成实例下，共用一个已登录的 token。

添加 / 删除方式（全程不需要再微信授权）：

1. HA → 设置 → 设备与服务 → Towngas → **配置（选项）**；
2. 选择 **管理户号（添加 / 删除）**；
3. **添加户号**：填写新的 `subs_id`（必填，用于读数）与 `subs_code`（选填，用于账单/余额），保存后集成自动重载，该户号的 8 个传感器会出现（设备名形如 `港华燃气 <subs_code>`）；
4. **删除户号**：从下拉里选要移除的户号，其传感器会一并移除。

> 多个户号共享同一个「微信用户级」token：只要该 token 有效，所有户号都能正常拉数；只有当 token 本身（微信用户级）失效时，才需要重新走一次微信登录（重新认证整个实例）。

## 校准账单 / 余额字段（已校准）

读数（本期/上期表数）与账单类字段均已用真实户号数据完成校准，可直接出数：

- **金额单位确认为「元」**：账单金额 `chrgSum`、余额 `availableBalance` / `savingSum`、欠费 `totalUnpaidFee` 均按原始值（元）透传，**无需 ÷100**。交叉验证：`price(2.85) × amount(23) = chrgSum(65.55)`。
- **本期用气** = 本期表数 `currReading` − 上期表数 `lastReading`（上期取自最近账单 `lastReading`）。
- **历史账单**经 `queryHistoryFee` → 账期 `datas[]` → 真实账单 `gasFeeList[]` 摊平取数。
- **账户余额 / 欠费**读自 `gasFeeBaseinfo` 平铺结构（预付费户读 `savingSum`，后付费户读 `availableBalance` + 欠费笔数）。

若日后切换户号仍出现 `unknown`，可调用开发者工具 → 服务 → **`towngas.dump_raw`**（可对全部户号跑）把原始 JSON 贴回自查。

## 系统维护窗口

港华燃气系统每日 **23:30–00:30（CST）** 为系统维护窗口：

- 此期间集成**自动跳过数据请求**，所有传感器保持上一轮的成功读数不变，不会出现临时不可用或报错；
- **Token 健康检查不受维护窗口影响**，仍按 `token_refresh_interval` 正常执行（维护期间若服务器返回网络错误会被忽略，仅当 token 真正过期时才提示重新认证）。

## 工作原理

集成直接调用港华燃气微信中央 VCC 网关（逆向自其 H5 前端 `weixin.towngasvcc.com/h5-gas`），并**参考了杭州港华（杭州燃气）开源项目 `palafin02back/hztowngas` 的相关说明**——其微信 OAuth 鉴权流程、请求签名算法、户号字段（`subsId` / `subsCode` / `orgCode`）命名，以及读数/账单接口模型，均作为本集成的主要实现依据。

### 鉴权（微信 OAuth，`/vcc-oauth/oauth/authorize2/...`）

| 用途 | 接口 |
|---|---|
| 登录授权入口（手机微信打开） | `GET /oauth/authorize2/union?clientid=...&redirectUri=...` |
| 用 `code` 换发 token | `POST /oauth/authorize2/accessToekn?authCode=<code>` |
| 刷新 token | `POST /oauth/authorize2/refreshToken?timestamp=<ms>&refreshToken=<rt>&sign=<sign>` |

- `access_token` 寿命 **7200 秒**；`refresh_token` 天级、可稳定多日。
- 刷新签名：`sign = MD5(排序后的 key+value 拼接 + SALT "hbasesoft.com-prod").upper()`。

### 数据（`/nv1/vcc-cbs/*`，GET，带 `Authorization: Bearer <access_token>` + `timestamp`+`sign`）

| 用途 | 接口 |
|---|---|
| 本期表数（读数，核心） | `GET /charge/preCheck`（参数 `subsId`，回退 `subsCode`+`orgCode`） |
| 历史账单 | `GET /charge/queryHistoryFee` |
| 账户余额 | `GET /charge/gasFeeBaseinfo` |
| 登录存活校验 | `GET /usersubs/getLoginUserInfo` |

> 注意：营业厅网关（`maanshan.towngasvcc.com/openapi/uv1`）与微信中央网关（本集成）的 token **不互通**，请勿把营业厅网页本地存储里的 token 贴进来——会被拒（20001）。

## 常见问题

- **提示 token 无效/过期**：正常情况下集成会自动续期 token，无需任何操作。只有当 `refresh_token` 也失效时才需要重新在微信里打开登录链接，把新的 `?code=` 回跳网址贴到「重新认证」。
- **找不到户号（subs_id）**：户号在港华燃气微信小程序 / 公众号「我的户号」里查看；或看每月账单推送里的户号。集成无法自动发现（机构参数服务端才认得）。
- **账单/余额显示不对或为空**：读数（表数）已稳；账单类字段已用真实数据校准（金额单位为元）。若仍异常，多半是 `subs_code` / `org_code` 未填或填错，可跑 `towngas.dump_raw` 把原始 JSON 贴回自查。
- **数据何时更新**：抄表/出账后网关数据更新，集成按设定的间隔轮询。

## 发版（维护者）

本仓库用 GitHub Actions 自动打包发版：

```bash
# 1. 本地提交改动
git add -A && git commit -m "v1.5.0: ..."
# 2. 打 tag 推送（tag 名必须以 v 开头，如 v1.5.0）
git tag v1.5.0
git push && git push --tags
```

推送 `v*` tag 后，GitHub Actions 会自动把 `custom_components/towngas/` 打包成 `towngas.zip` 并创建 Release。HACS 检测到新 commit / Release 后即可「重新下载」更新。

## 参考与致谢

- **杭州港华（杭州燃气）开源项目 [`palafin02back/hztowngas`](https://github.com/palafin02back/hztowngas)**：本集成的微信 OAuth 鉴权流程、请求签名算法、户号字段（`subsId` / `subsCode` / `orgCode`）命名及读数/账单接口模型，主要参考该项目的说明与实现，在此向其作者致谢。

## 免责声明

本集成为个人逆向开发的非官方集成，仅供学习使用。接口变更可能导致集成失效。
