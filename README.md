<div align="center">
  <img src="assets/nailong-bot.png" width="180" alt="奶龙">
  <h1>奶龙体育助手</h1>
  <p><b>让奶龙帮你完成 HUST 校园跑吧！</b></p>
  <p>
    <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue.svg"></a>
    <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-blue">
    <img alt="App" src="https://img.shields.io/badge/HUST%E4%BD%93%E8%82%B2-1.0.9-lightgrey">
  </p>
</div>

奶龙体育助手是一条**纯协议**的 HUST 校园跑上传链路：

- **核心**：CAS 登录 → 领取本次运动任务 → 按路线生成一条跑步记录 → 等记录时间真正走完 → 本地签名并提交 → 补传传感器数据。不需要手机、模拟器或改包 App。
- **可选**：基于 [easybot](https://github.com/SaucePlum/easybot) 的 QQ 机器人，私聊完成登录/上传/看日志，给朋友发邀请码各自用自己的账号，每天 10:00 自动串行上传。

> 本项目仅用于个人学习与协议研究。请遵守学校体育考核规定，只用自己的账号，风险自负。

---

## 快速开始（核心上传）

```bash
python -m pip install -r requirements.txt

cp config.example.yaml config.yaml        # 按需改路径与设备指纹
python -m hustrun login                   # 学号 + 密码 + 验证码/动态码
python -m hustrun check                   # 只读自检：会话 / 服务端时间 / 时段
```

正式提交：

```bash
python -m hustrun upload -r config/routes/route_track.json -d 2150
```

`upload` 会原地等记录时间走完再提交（2.15 km ≈ 8 分钟）。不想让进程一直
挂着，可以拆成两段：

```bash
python -m hustrun prepare -r config/routes/route_track.json -d 2150   # 领任务 + 生成记录
python -m hustrun submit      # 到点后提交，默认取最新 session
```

只看生成的数据（不联网、不签名）：

```bash
python -m hustrun generate -r config/routes/route_track.json -d 2150 --out payload.json
python -m hustrun routes      # 列出 config/routes 下每条路线的长度
```

> 记录时间必须真实走完才能提交；`prepare` 只是把「领取 + 生成」和「提交」
> 拆成两步，中间的时间不能压缩。

---

## 可选的 QQ 机器人（多用户）

`qqbot/` 提供一个只处理**私聊**的 easybot 机器人：

| 你会说 | 它做什么 |
|---|---|
| `登录` | CAS 登录：验证码图片直接发到私聊，首次还会问学号和密码 |
| `上传` → `确认` | 立即上传一条 2.05 km 记录（需二次确认） |
| `状态` / `日志` | 只读自检 / 最近上传日志 |
| `邀请 备注` | owner 生成 24 小时有效、单次的邀请码 |
| `绑定 <邀请码>` | 朋友绑定后自动建独立目录，默认每天 10:00 自动上传 |
| `用户列表` / `添加用户` / `删除用户` / `设置自动` | owner 管理 |

每个用户一个目录 `users/<openid>/`，自己的 `config.yaml`、`HEADER.xml`、
`USERINFO.xml`、`cas.json`、路线文件，互不共用凭证。AppSecret、CAS 密码、
TOKEN、SECRET 只落在 600 权限的文件里，不写日志。

部署（以仓库放在 `/opt/hustrun` 为例）：

```bash
python -m pip install -r requirements.txt -r qqbot/requirements.txt

cp qqbot/bot.env.example qqbot/bot.env     # 填 AppID/AppSecret，然后 chmod 600
cp deploy/hustrun-qqbot.service /etc/systemd/system/
cp deploy/hustrun-upload.service deploy/hustrun-upload.timer /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now hustrun-qqbot.service
systemctl enable --now hustrun-upload.timer
```

`bot.env` 里的 `BIND_CODE` 用来把第一个私聊的 openid 绑成 owner；也可以在
`users.json` 里直接写（结构见 `qqbot/users.example.json`）。

目录默认就是仓库本身：用户目录 `users/<openid>/`、日志 `logs/`。换布局时
用 `HUSTRUN_ROOT`、`HUSTRUN_APP_DIR`、`HUSTRUN_QQBOT_DIR`、
`HUSTRUN_USER_ROOT`、`HUSTRUN_USERS_FILE`、`HUSTRUN_LOG_DIR`、
`HUSTRUN_PYTHON` 覆盖（`deploy/` 里的单元文件有注释示例）。

---

## 目录

```
hustrun/              核心实现：登录、签名、轨迹生成、上传流水线
qqbot/                QQ 机器人（bot.py）与每日 10:00 串行上传（daily_upload.py）
tests/                生成器 / 签名 / 流水线单测
config/routes/        轨迹 JSON（操场、体育场、手画路线）
config.example.yaml   配置示例
deploy/               systemd 单元示例（机器人 + 每日定时上传）
scripts/              路线生成脚本
assets/               README 用图
```

## 依赖与限制

- Python 3.10+；核心 `requests`、`pycryptodome`、`PyYAML`、`click`；
  机器人额外需要 `easybot-qq`
- 只在 06:00–23:00 时段、单次 ≥2.0 km 的规则下有效
- 提交后是「待审」，学校审核后才变成「有效」
- TOKEN 约 7 天过期（`code=2001`），重新 `login` 即可
- 适配 App 1.0.9 的协议；服务端升级后可能需要重新抓包

## 隐私与安全

- `.credentials/`、`config.yaml`、`.cache/`、`.sessions/`、`qqbot/bot.env`、
  `qqbot/users.json`、`qqbot/cas.json` 均被 `.gitignore` 忽略，不要提交。
- TOKEN / SECRET / 签名结果不写入日志；异常只报告长度或格式。
- 只对自己的账号使用；上传会改变服务端状态。

## 免责声明

见 [DISCLAIMER](DISCLAIMER)。代码 MIT。
