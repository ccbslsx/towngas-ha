# Towngas 港华燃气 · Home Assistant 自定义集成

从港华燃气「网上营业厅」（默认马鞍山 `https://maanshan.towngasvcc.com/`，其他城市营业厅地址也可在配置时修改）拉取账单数据，为每个燃气户号创建一个设备，包含以下传感器：

| 传感器 | 说明 | 单位 |
|---|---|---|
| 本期用气 | 最近一个账期的用气量（附阶梯明细属性） | m³ |
| 本期表数 | 最近一次抄表读数 | m³ |
| 上期表数 | 上一次抄表读数 | m³ |
| 本期账单金额 | 最近账期金额（附违约金 / 余额抵扣等属性） | ¥ |
| 用气单价 | 第一阶梯单价 | ¥/m³ |
| 账户余额 | 账户剩余金额 | ¥ |
| 欠费金额 | 当前欠费总额（附欠费笔数属性） | ¥ |
| 账期 | 最近账期（如 2026-07） | - |

## 安装

### 方式一：手动安装（HACS 用户可用方式二）

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
# 然后把下面两行里的 <你的用户名> 换成实际用户名，执行：
git remote add origin https://github.com/<你的用户名>/towngas-ha.git
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
3. 仓库地址填 `https://github.com/<你的用户名>/towngas-ha`，**类别选 Integration**；
4. 点击「添加」。

#### 第 3 步：安装集成

1. HACS → 集成 → 右下角 **浏览 (Explore)** / 搜索 **Towngas 港华燃气**；
2. 点开 → **下载 (Download)**，等待完成；
3. 重启 Home Assistant；
4. 按上方「配置」章节添加集成、粘贴 token、勾选户号。

> HACS 自定义仓库方式**无需**把集成提交到 HACS 官方默认列表，自己用完全够；日后 `git push` 新版本后，HACS 会提示可更新，一键升级即可。
>
> 若想进 HACS 默认仓库（别人也能直接搜到），需向 `hacs/default` 提交 PR，门槛更高，个人使用不推荐。

## 配置

### 第 1 步：获取 Token（关键步骤）

营业厅登录使用手机号 + 短信验证码（SSO），无法在 HA 里自动完成，因此采用「浏览器登录后复制 token」的方式：

1. 电脑浏览器打开 `https://maanshan.towngasvcc.com/`，用手机号 + 短信验证码登录；
2. 登录成功后按 **F12** 打开开发者工具；
3. 切到 **应用 (Application) → 本地存储 (Local Storage) → `https://maanshan.towngasvcc.com`**；
4. 找到键 **`token`**，复制它的值，形如：

   ```json
   {"access_token":"8a1b2c3d-....","refresh_token":"9z8y7x...."}
   ```

5. 把整段 JSON 粘贴到集成配置里即可（只粘贴 access_token 的值也可以）。

> Token 有效期由服务端决定，过期后集成的传感器会变成不可用，并在 HA 的「需要 attention 的配置项」中提示重新认证，按提示粘贴新 token 即可。

### 第 2 步：添加集成

1. HA → 设置 → 设备与服务 → 添加集成 → 搜索 **Towngas** 或 **港华燃气**；
2. 粘贴 token（营业厅地址已预填马鞍山，其他城市可修改）；
3. 集成会列出账号下绑定的所有户号（多选），确认后每个户号生成一个设备。

### 配置项

HA → 设置 → 设备与服务 → Towngas → 配置（右上角「配置」），可设置两个间隔：

| 配置项 | 说明 | 允许范围 | 默认 |
|---|---|---|---|
| `scan_interval`（数据刷新间隔） | 多久轮询一次账单/余额/欠费 | 60–86400 秒 | 21600 秒（6 小时） |
| `token_refresh_interval`（Token 刷新间隔） | 多久检查一次 token 是否仍有效 | 300–7140 秒 | 1800 秒（30 分钟） |

> - 间隔都用**秒**为单位。账单数据按月更新，数据刷新无需太频繁，默认 6 小时足够。
> - Token 刷新间隔用于**提前发现 token 过期**并弹出重新认证。注意：港华营业厅**没有**可供程序调用的 token 刷新接口（前端过期即跳登录），所以「刷新」实际是「健康检查」——检测到过期就提示你重新粘贴 token，而不会自动续期。
> - 修改配置项后会自动重载集成。

## 系统维护窗口

港华燃气系统每日 **23:30–00:30（CST）** 为系统维护窗口：

- 此期间集成**自动跳过数据请求**，所有传感器保持上一轮的成功读数不变，不会出现临时不可用或报错；
- **Token 健康检查不受维护窗口影响**，仍按 `token_refresh_interval` 正常执行（维护期间若服务器返回网络错误会被忽略，仅当 token 真正过期时才提示重新认证）。

## 工作原理

集成直接调用网上营业厅前端使用的 openapi（逆向自其前端代码）：

| 用途 | 接口 |
|---|---|
| 绑定户号列表 | `GET /openapi/uv1/user/queryBindSubsLimitServer` |
| 账单列表 | `GET /openapi/uv1/bill/queryBills` |
| 账户余额 | `GET /openapi/uv1/acct/queryAcctRes` |
| 欠费 | `GET /openapi/uv1/bill/queryUnpaidBills` |
| 户信息 | `GET /openapi/uv1/subs/querySubs` |

请求格式：`?seq=<接口码+时间戳+随机数>&token=<access_token>&client_id=<web客户端id>`。

## 常见问题

- **提示 token 无效/过期**：重新登录营业厅，按第 1 步复制新 token，点集成里的「重新认证」。
- **其他城市港华**：营业厅前端全国共用一套代码，理论上把「营业厅地址」改成对应城市的域名（如 `https://xxx.towngasvcc.com/`）即可，未逐一验证。
- **数据何时更新**：抄表/出账后营业厅数据更新，集成按设定的间隔轮询。

## 免责声明

本集成为个人逆向开发的非官方集成，仅供学习使用。接口变更可能导致集成失效。
