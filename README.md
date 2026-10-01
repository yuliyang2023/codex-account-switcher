# Codex 多账号切换工具

支持 Linux/macOS 的 Python 3.8+、Bash/Zsh，无需安装 Python 依赖。
各账号的凭据分别保存，运行时共用一个 CODEX_HOME；切换后可以继续原会话。
账号编号支持任意正整数（1、2、3……），没有两个账号的数量限制。

## Zsh 插件安装

需要已安装 Python 3.8+ 和 Codex CLI。插件加载时只定义命令，不自动登录或切换账号。

### Oh My Zsh

```zsh
git clone https://github.com/yuliyang2023/codex-account-switcher.git \
  "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/codex-account-switcher"
```

在 `~/.zshrc` 已有的 `plugins=(...)` 中添加 `codex-account-switcher`，保留其他插件，例如：

```zsh
plugins=(git codex-account-switcher)
```

插件列表应放在 `source "$ZSH/oh-my-zsh.sh"` 前。打开新终端或执行 `exec zsh` 后使用：

```zsh
cxs init
cxs import 1 --yes
cxs login 2 --browser-auth
cxs login 3 --browser-auth
cxs use 3
codex
```

更新插件：

```zsh
git -C "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/codex-account-switcher" pull --ff-only
```

更新后打开新终端。卸载时从 `plugins=(...)` 删除 `codex-account-switcher` 并打开新终端；
账号凭据仍保存在 `~/.codex-switcher`。

### 普通 Zsh（无需 Oh My Zsh）

```zsh
git clone https://github.com/yuliyang2023/codex-account-switcher.git \
  "$HOME/.zsh/plugins/codex-account-switcher"
```

将下面一行添加到 `~/.zshrc`，再打开新终端：

```zsh
source "$HOME/.zsh/plugins/codex-account-switcher/codex-account-switcher.plugin.zsh"
```

更新插件并重新加载：

```zsh
git -C "$HOME/.zsh/plugins/codex-account-switcher" pull --ff-only
source "$HOME/.zsh/plugins/codex-account-switcher/codex-account-switcher.plugin.zsh"
```

如果安装在其他目录，将命令中的路径替换为实际安装目录。更新无需重新登录账号；
`cxs quota` 每次执行都会读取安装目录中的 Python 脚本，更新脚本后即可使用新的额度输出。

两种方式均提供 `cxs` 和 `codex` 函数；`codex` 会使用切换工具选定的账号。
插件入口采用标准的 `<插件名>.plugin.zsh` 文件名，安装约定参考
[Oh My Zsh 自定义插件文档](https://github.com/ohmyzsh/ohmyzsh/wiki/Customization#overriding-and-adding-plugins)。

## 开始使用

在工具目录执行（如果放到了其他机器，先解压并进入该目录）：

```bash
./cxs init
# 导入已经登录的账号 1，同时记录现有会话目录
./cxs import 1 --yes
# 登录账号 2，浏览器中确认选择第二个账号
./cxs login 2 --browser-auth
./cxs status

# 启动账号 1
./cxs use 1
./cxs run
```

远程机器可以使用 `./cxs login 2`，默认使用设备码登录，按终端提示完成认证。
导入要求源目录存在 auth.json；如果账号使用系统钥匙串，请先以文件模式登录：
`codex -c 'cli_auth_credentials_store="file"' login`。

继续添加第三个或更多账号，无需重新初始化或迁移已有账号：

```bash
./cxs login 3 --browser-auth
./cxs login 4 --browser-auth
# 也可以从已有登录目录导入
./cxs import 5 --source /path/to/codex-home --yes
./cxs status
./cxs use 3
./cxs run
# 退出 Codex 后，切换并继续最近会话
./cxs resume 4
```

`login` 和 `import` 自动创建对应编号的凭据目录；`status` 和 `quota` 自动列出全部账号。

## 切换并继续原会话

用 `/quit` 退出正在运行的 Codex，在同一个项目目录执行：

```bash
# 指定工具绝对路径，或使用下方 source 后提供的 cxs 命令
/apps/yly/codex-account-switcher/cxs resume 2
# 切回账号 1，同样继续会话
/apps/yly/codex-account-switcher/cxs resume 1
```

`resume` 默认恢复当前项目的最近会话，可指定 `resume 2 --session <会话ID>`。
没有记录的会话无法恢复，第一次开始新任务使用 `run`。
这是分别保存本地凭据并组合 Codex resume 的工具，不是进程内热切换；
仍须退出当前 CLI 进程，各账号无需执行 logout。

## Shell 快捷命令

```bash
source /apps/yly/codex-account-switcher/codex-switcher.zsh
cxs use 1
codex
# /quit 后
cxs resume 2
```

脚本适用于 Bash 和 Zsh。需要长期使用，可将 source 行加到 ~/.bashrc 或 ~/.zshrc。
它会在当前 shell 中定义 codex 函数，将启动请求交给切换工具。
独立使用时直接调用 ./cxs，无需修改 shell 配置。

## 命令

| 命令 | 作用 |
| --- | --- |
| init | 初始化凭据目录，保留默认槽位 1 和 2；更多槽位按需创建 |
| import 1 --yes | 导入当前 CODEX_HOME 或 ~/.codex 的登录凭据及会话位置 |
| import 1 --source /path --yes | 导入指定目录 |
| login 2 --browser-auth | 浏览器登录账号 2 |
| login 2 | 设备码登录账号 2 |
| login 3 --browser-auth | 添加并登录账号 3；可使用更多正整数编号 |
| status | 查看账号状态和邮箱，不显示令牌 |
| quota | 查询全部账号的额度、邮箱和可用重置的到期时间，依赖 CLI app-server 接口 |
| quota 3 | 只查询账号 3 的额度 |
| use 2 | 设置下一次启动使用账号 2 |
| run | 启动新会话 |
| run resume --last | 恢复当前项目最近会话 |
| resume 2 | 切换至账号 2 并恢复最近会话 |
| resume 2 --session ID | 切换并恢复指定会话 |
| current | 显示默认账号槽位 |
| logout 2 | 删除账号 2 的登录凭据 |

## 数据与行为

- 凭据槽位默认位于 ~/.codex-switcher/accounts/<账号编号>/auth.json。
- 会话目录默认 ~/.codex；import 会记录导入来源，保留该目录里的会话和配置。
- 未 import 时，可设置 CODEX_SESSION_HOME 指定共用会话目录；import 保存的位置优先。
- 运行时将所选账号凭据原子写入共用目录的 auth.json，退出后保存刷新后的凭据到对应槽位。
- 首次启动前，共用目录原有的 auth.json 备份为 auth.before-switcher.json。
- 凭据目录权限 700，写入的凭据文件权限 600；不要将凭据上传或提交到 Git。
- 检测到 CLI 支持 --no-daemon 时，交互启动及 resume/fork 自动加入该选项，避免旧后台账号状态。
- 文件锁阻止多个通过本工具启动的 Codex 同时使用共用目录。
  工具不能锁住外部启动的 CLI/IDE；切换、登录、导入和查询额度前，先关闭这些进程。
- 工具不自动切换额度用完的账号，也不清理或迁移会话内容。

可用 CODEX_SWITCHER_HOME/--root 指定凭据槽位根目录，CODEX_BIN 指定 Codex 可执行文件。

## 验证

```bash
python3 -m unittest discover -s tests -v
```

测试使用模拟 Codex 验证凭据刷新、失败退出时保存凭据、会话保留和并发阻止；
还验证多账号添加、状态和额度列表，以及第三个账号的会话恢复和凭据刷新；
不调用真实账号或消耗额度。真实跨账号续聊仍需在你的账号上验证。
