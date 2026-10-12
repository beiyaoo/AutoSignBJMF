# 班级魔方多人GPS自动签到

- Thanks To [JasonYANG170/AutoCheckBJMF](https://github.com/JasonYANG170/AutoCheckBJMF) ，根据自己学校的签到进行了简化
- 仅根据自己学校的班级魔方需求更改简化代码,仅支持GPS签到(可在范围外)，其他功能请到项目[AutoCheckBJMF](https://github.com/JasonYANG170/AutoCheckBJMF)项目查看其他内容
- 可配置多人签到
- 可配置QQ/WX通知签到情况
- 如果你觉得好用,`Please Star`orz
  ![网页端展示](doc/img0.jpg)
## 代码结构

项目已进行模块化重构，提高了代码的可维护性和可读性：

```
BJMF/
├── BJMF.py                 # 主程序，负责整体流程控制
├── auto_add_user.py        # 自动添加用户工具，通过微信扫码获取用户信息并写入配置data.json
├── .env                    # (可选) 环境变量配置文件，用于配置公共参数
└── utils/                  # 工具模块目录
    ├── __init__.py         # 模块初始化文件
    ├── config_manager.py   # 配置文件管理模块
    ├── user_info.py        # 用户信息获取模块
    ├── notification.py     # 通知发送模块
    └── attendance.py       # 签到任务执行模块
```

### 各模块职责

- **BJMF.py**: 主程序入口，负责读取配置、遍历用户、调用签到任务
- **auto_add_user.py**: 自动添加用户工具，通过微信扫码获取用户信息并写入data.json配置文件；支持读取.env公共配置
- **config_manager.py**: 处理配置文件的读取和保存
- **user_info.py**: 获取用户信息和班级信息
- **notification.py**: 处理QQ和微信消息发送
- **attendance.py**: 执行签到任务的核心逻辑

## 功能

- 自动从指定课程中获取签到项
- 通过模拟表单提交，实现自动签到
- 签到成功后,发送QQ/WX消息通知(可选配,可以不用配置)
- 支持通过微信扫码快速添加用户
- 支持 `.env` 配置公共参数，简化多人配置

## 更新说明

- 2026.09.01
  - 作者学校的签到方式改变，本项目停止维护，但功能依旧正常，如有需要可以自行二开优化使用。

- 2026.01.10
  - `auto_add_user.py` 优化: 支持通过 `.env` 文件配置公共参数(经纬度、通知Key等)，简化配置流程
  - `auto_add_user.py` 优化: 增加二维码自动清理机制，避免垃圾文件堆积及文件占用问题
  - `utils/attendance.py` 修复: 优化签到状态检测逻辑，增加对"已签到"状态的HTML解析，解决正则匹配失败导致的误报问题

- 2025.12.15
  - 更新 `utils/attendance.py` ,改用 requests.Session()防止获取签到项失败问题；同时增加了对 response.url 的检测

- 2025.12.04 v2版本
  - 新增 `auto_add_user.py` 工具，实现微信扫码自动获取用户信息并写入配置文件data.json
  - 简化了用户添加流程，无需手动获取Cookie和班级ID

## 安装依赖

在使用该脚本之前，请确保安装以下依赖项：
```bash
pip install -r requirements.txt
```

## 配置指南

### 方法一：自动添加用户 (推荐)

最简单的方法，无需手动抓包或查找 Cookie。

1. **(可选) 配置公共参数**：
   在项目根目录下创建 `.env` 文件（可参考下方模板），填入经纬度等公共信息。
   ```properties
   # 是否启用公共配置 (True/False)
   ENABLE_COMMON_CONFIG=True

   # 公共配置参数
   COMMON_LAT=xxxxxx  # 纬度
   COMMON_LNG=xxxxxx  # 经度
   COMMON_ACC=30      # 精度
   COMMON_QMSG_KEY=   # Qmsg推送Key(可选)
   COMMON_WX_KEY=     # Server酱推送Key(可选)
   ```

2. **运行自动工具**：
   ```bash
   python auto_add_user.py
   ```

3. **扫码登录**：
   使用微信扫描弹出的二维码，程序会自动获取 Cookie 和班级信息并保存到 `data.json`。

### 方法二：手动配置 (高级)

如果你需要手动配置 `data.json`，可以按照以下步骤获取参数。

#### 1. data.json 格式
```json
{
    "students": [
        {
            "name": "用户备注",
            "class": "110141",
            "lat": "30.123456",
            "lng": "120.123456",
            "acc": "30",
            "cookie": "从浏览器获取的Cookie字符串",
            "QmsgKEY": "",
            "WXKey": ""
        }
    ]
}
```

#### 2. 参数获取说明

- **Cookie 获取方法 (PC端浏览器)**：
  1. 电脑微信登录并打开签到页面：`http://g8n.cn/student/login?ref=%2Fstudent`
  2. 按 `F12` 打开开发者工具，切换到 **网络 (Network)** 标签。
  3. 刷新页面，在左侧请求列表中找到第一个请求（通常是数字或 student）。
  4. 点击该请求，在右侧 **标头 (Headers)** -> **请求标头 (Request Headers)** 中找到 `Cookie`。
  5. 复制 `Cookie:` 后面的所有内容（通常以 `PHPSESSID=...` 或 `remember_student_...` 开头）。
  
  ![浏览器查看Cookie](doc/img5.jpg)

- **Class (班级ID)**：
  - 在上述浏览器页面的网址栏中，可以看到类似 `course/110141` 的内容，`110141` 即为班级ID。
  - 或者在页面中查看。
  ![浏览器查看班级码](doc/img4.jpg)

- **经纬度 (Lat/Lng)**：
  - 使用 [高德地图坐标拾取器](https://lbs.amap.com/tools/picker) 获取。

- **推送 Key (可选)**：
  - **Qmsg酱**: [官网](https://qmsg.zendee.cn/) 注册获取 Key。
  - **Server酱**: [官网](https://sct.ftqq.com/) 扫码获取 Key。

## 使用方法

1. **配置用户**：使用上述任意一种方法完成用户配置（生成 `data.json`）。
2. **执行签到**：
   ```bash
   python BJMF.py
   ```
3. **自动化运行**：
   - **Windows**: 使用"任务计划程序"设置定时任务。
   - **Linux**: 使用 `crontab` 设置定时任务。
   - **云函数**: 可部署至云函数平台。

## 文件与隐私说明

- `data.json`、`.env` 等文件中包含个人 Cookie、经纬度、推送 Key 等敏感信息，**不会被提交到 Git 仓库**（已在 `.gitignore` 中忽略），请妥善保管本地副本并做好备份。
- `web_signin/` 目录下的本地数据库文件 `web_signin/db/web_signin.db` 也已默认加入 `.gitignore`，仅用于本地运行和调试，不会上传到远程仓库。
- 如需分享或上传日志/截图，请注意**手动打码或删除其中的 Cookie、班级 ID、经纬度等个人隐私信息**。

## web_signin Web 管理端

`web_signin/` 提供了一个基于 Flask + 前端页面的**Web 管理界面**，用于在浏览器中管理用户、查看签到任务等。

### 本地直接运行

1. 安装 Web 版依赖（在项目根目录执行）：
   ```bash
   pip install -r web_signin/requirements.txt
   ```
2. （可选）在 `web_signin/` 目录下复制一份配置模板：
   ```bash
   cd web_signin
   copy config.json.example config.json  # Windows
   # 或
   cp config.json.example config.json    # Linux / macOS
   ```
   如需自定义 Web 端口，可在 `config.json` 中填写 `"port": 9988` 等数值，或通过环境变量 `WEB_SIGNIN_PORT` 覆盖。
   如需使用管理员后台，请填写 `admin_password`，`admin_username` 默认为 `admin`。
   也可使用环境变量 `ADMIN_PASSWORD` / `ADMIN_USERNAME`，环境变量优先。
3. 启动 Web 服务：
   ```bash
   python web_signin/run.py
   ```
4. 在浏览器访问：
   ```text
   http://127.0.0.1:9988
   ```
   若修改了端口，请将 `9988` 替换为实际端口。

### 管理员登录与运行状态

用户配置页右上角的「管理员登录」进入 `/admin`，原 `/manage` 地址也可使用。
首次使用时，在 `web_signin/config.json` 中设置自己的管理员密码，例如：

```json
{
  "port": 9988,
  "admin_username": "admin",
  "admin_password": "请替换为自己的长密码"
}
```

保存后重启 Web 服务，使用配置的账号和密码登录。`config.json` 已加入 Git 忽略列表。
没有配置密码时，后台禁止访问任务数据；不提供默认密码，也不能通过公开的 `/api/admin/init` 抢先创建管理员。
密码以带盐哈希保存到数据库。修改配置密码后重启即可更新；已有管理员 Key 仍可登录，并在登录成功后升级旧哈希。

登录后可以：

- 查看所有签到任务的姓名、班级、坐标、每日执行时间和生效日期。
- 查看「正在执行」「等待调度」「尚未生效」「已到期」「已禁用」等状态，以及下次执行时间和最近签到结果。
- 按姓名、班级、启用状态和执行状态筛选任务，选中任务查看最近 50 条执行日志。
- 修改配置、启用或禁用任务，以及删除任务和日志。修改时间或日期后调度立即更新。

页面每 10 秒刷新运行状态和所选任务日志，时间按北京时间显示。
「正在执行」指本 Web 服务正在调用签到流程；「等待调度」指任务已注册，等待下一次定时执行。
后台仅统计 Web 服务数据库中的任务，独立运行 `BJMF.py` 的任务不在此列表内。
禁用任务会停止后续调度，已经开始的执行会继续完成。

管理员 token 保存在当前浏览器标签页的会话存储中，24 小时过期；退出会立即撤销 token。
管理接口需要 `X-Admin-Token` 请求头，普通用户只能凭自己的扫码会话读取任务和日志。
Cookie 不会返回到浏览器。管理员登录及实时执行状态沿用单进程内存管理，重启后需重新登录；部署时使用单个 Web 进程。

验证管理员功能（在项目根目录执行）：

```bash
python -m unittest discover -s tests -v
```

测试使用临时数据库和模拟签到，不调用真实签到或通知接口。

### 使用 Docker 运行

项目内已提供 `web_signin/Dockerfile`，可用于快速构建和运行 Web 版：

```bash
docker build -t bjmf-web -f web_signin/Dockerfile .
docker run -d --name bjmf-web -p 9988:9988 -e ADMIN_PASSWORD="请替换为自己的长密码" -v ./web_signin/db:/app/web_signin/db bjmf-web
```

如需调整端口，可修改映射 `-p 宿主端口:容器端口`，或通过环境变量 `WEB_SIGNIN_PORT` 覆盖容器内部端口。

使用 `docker compose up -d --build` 时，可在根目录 `.env` 中配置 `ADMIN_USERNAME=admin` 和 `ADMIN_PASSWORD=自己的长密码`。
Compose 会传入管理员配置并持久化数据库；用户扫码创建的任务和管理员密码哈希在重建容器后保留。

## 注意事项

- 程序会自动检测并填充空的 class 字段。
- 签到二维码/Cookie 具有时效性，如果签到失败（提示 Cookie 无效），请重新运行 `auto_add_user.py` 更新凭证。
