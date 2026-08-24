# Git 推送操作指引（WSL → GitHub）

> 固化日期：2026-08-24
> 场景：本项目在 Windows 的 `D:\HCM_ASST`，通过 WSL 的 git 推送代码到 GitHub。

---

## 一、关键问题：WSL 连不上 github.com（HTTPS/SSH 均超时）

**现象**：`git push` 到 `github.com` 报：
- `GnuTLS recv error (-110): TLS connection was non-properly terminated`
- 或 `Failed to connect to github.com port 443 ... Couldn't connect to server`

**实测诊断结论**：
- `api.github.com`、`codeload.github.com` 均可用（HTTP 200）。
- `github.com` 主域 HTTPS **稳定失败**（TCP 443 能建连，但 TLS/HTTP 被网络阻断）。
- github.com SSH（22 / ssh.github.com:443）也超时。

**根因**：WSL 默认 **IPv6 优先**，`github.com` 的 IPv6 路径被网络环境阻断 → 所有走 github.com 的请求超时。强制 IPv4 + 正确 IP 后即通。

---

## 二、解法：/etc/hosts 强制 IPv4

把 `github.com` 固定解析到已验证可通的 IPv4 地址：

```bash
# 在 WSL 里执行（需要 sudo）
echo "20.205.243.166 github.com" | sudo tee -a /etc/hosts
```

**验证**：
```bash
getent hosts github.com   # 应显示 20.205.243.166
curl -s -o /dev/null -w "%{http_code}\n" --resolve github.com:443:20.205.243.166 "https://github.com"   # 应 200
```

> 注意：`20.205.243.166` 是 github.com 的一个亚洲/可通 IP，**可能随网络环境变化**。若失效，用 `getent ahostsv4 github.com` 列出各 IP，逐测可通者再写入 hosts。

---

## 三、推送步骤（一次性的标准流程）

### 1. 提交改动（用 WSL git）
```bash
cd /mnt/d/HCM_ASST
git add <文件...>
git commit -m "描述"
```

### 2. 避免行尾符噪音（重要）
Windows 项目在 WSL git 下常出现 CRLF/LF 对称改动（`git diff` 显示整文件增删）。先规范化：
```bash
git config core.autocrlf input
git add --renormalize <文件...>
```

### 3. 推送（用 PAT 认证）
```bash
TOKEN="<你的 GitHub PAT>"
git push "https://${TOKEN}@github.com/nipin0/Hcm.git" main
```

> **安全**：token 只通过命令行临时传入，**勿写入 git remote URL 持久化、勿提交进仓库**。建议用完在 GitHub 撤销重建，或配置 Windows Credential Manager 长期保存。

---

## 四、铁律/教训

> **铁律（WSL 访问 GitHub 必须先测 IPv4）**：WSL 下 github.com 超时 ≠ 网络不通，常是 **IPv6 路径被阻断**。第一排查动作：`curl --resolve github.com:443:<IPv4> "https://github.com"` 测 IPv4 是否可通；可通则写 `/etc/hosts` 强制 IPv4 后再 `git push`，**不要**反复重试浪费时间。

> 提交前先 `git add --renormalize` 消除行尾符噪音，避免把 CRLF/LF 对称改动混进 commit。

> PAT 是一次性敏感凭据：命令行内联使用、用完即弃，禁止持久化到 remote URL / 配置文件 / 仓库。
