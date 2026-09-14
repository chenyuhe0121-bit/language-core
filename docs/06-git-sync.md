# 同步到 GitHub

本地仓库已经建好，但还没有远程。下面分成「推上去」和「回去拉下来」两段。

密钥与数据不会进仓库：`data/llm.env`、`data/*.db`、`data/_*`、`.tmp/` 都在 `.gitignore` 里。
换电脑后需要自己重建 `data/llm.env`，内容参考 `.env.example`。

---

## 一、推上去（在公司这台机器上做一次）

### 1. 在 GitHub 网页上建空仓库

浏览器打开 https://github.com/new

- Repository name 随意，例如 `language-core`
- 选 **Private**（里面有产品设计，不建议公开）
- **不要**勾选 Add a README / .gitignore / license，否则会和本地冲突

建完页面上会给你一个地址，形如
`https://github.com/你的用户名/language-core.git`

### 2. 本地绑定并推送

```bash
cd D:\ai_video\language-core
git remote add origin https://github.com/你的用户名/language-core.git
git branch -M main
git push -u origin main
```

第一次推送会弹窗要求登录 GitHub。用浏览器登录一次即可，Windows 会把凭据存进
凭据管理器，以后不用重复登录。

推送成功后刷新仓库页面，应该能看到 31 个文件。

### 3. 确认密钥没有上传（重要）

在 GitHub 页面搜索框里搜 `sk-`，或者本地执行：

```bash
git log --all --full-history -- data/llm.env
```

这条命令应该**没有任何输出**。有输出说明 key 被提交过，需要立刻去 DeepSeek
后台重置密钥并清理历史。

---

## 二、回去拉下来（家里的电脑上）

### 1. 装 Git

https://git-scm.com/downloads

安装时一路默认即可。装完打开 Git Bash 或 PowerShell 验证：

```bash
git --version
```

### 2. 配置身份（只做一次）

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

### 3. 拉代码

```bash
cd D:\            # 或你想放的位置
git clone https://github.com/你的用户名/language-core.git
cd language-core
```

### 4. 重建密钥文件

```bash
mkdir data
```

新建 `data/llm.env`，内容：

```
LANGUAGE_CORE_LLM_API_KEY=你的key
LANGUAGE_CORE_LLM_BASE_URL=https://api.deepseek.com/v1
LANGUAGE_CORE_LLM_MODEL=deepseek-flash
```

`data/` 目录不存在时自动降级成 mock 模式，不会报错，但对话质量会掉。

### 5. 跑起来

```bash
python run.py --check    # 先验证模型通不通
python run.py            # 启动，浏览器开 http://127.0.0.1:8420
python tests/smoke.py    # 跑 41 项回归测试
```

需要 Python 3.9 以上。项目零第三方依赖，不用 pip 装任何东西。

---

## 三、以后两边同步

改动之后：

```bash
git add -A
git commit -m "说明这次改了什么"
git push
```

换到另一台机器时，先拉：

```bash
git pull
```

如果两边都改过同一个文件，会出现冲突。冲突文件里会有 `<<<<<<<` 标记，
手工改成你想要的样子，删掉标记，再 `git add` 和 `git commit`。

---

## 四、这个仓库里有什么

```
language-core/
├─ README.md              项目说明、运行方式、HTTP 接口
├─ run.py                 启动入口
├─ .env.example           密钥文件模板
├─ docs/                  5 份规格文档（愿景、技术栈、协议、架构、记忆导读）
├─ language_core/         内核代码（9 个模块）
│   ├─ assets/            Elise 角色卡 + 5 个场景卡
│   └─ web/               调试界面
├─ tests/smoke.py         41 项端到端测试
└─ tools/capture_prompt.py 提示词抓包工具
```

不在仓库里的：`data/`（密钥、数据库、调试输出）、`__pycache__/`、`.tmp/`。
