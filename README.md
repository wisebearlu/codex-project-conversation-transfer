# Codex Project Conversation Transfer

[中文](#中文) · [English](#english)

## 中文

### Codex 项目会话迁移

在两台 Windows 电脑之间离线迁移 Codex 本地项目的聊天记录。脚本自动识别项目和会话，连同分页历史、归档记录及项目归属写入一个 ZIP。项目代码可以继续通过 Git 同步。

迁移包包含会话正文、工具输出和原始路径等敏感数据。请私下传输和保存。脚本通过清单选择会话文件和索引，不收集项目目录、登录凭据、应用配置、Skills 或插件。

### 运行条件

- 两台电脑均使用 Windows 和 Python 3.14 或更新版本。`compression.zstd` 随 Python 提供。
- 两台电脑的 Codex 数据库结构需要兼容。尽量使用相同版本的 Codex。
- 正式迁移前，完全退出 ChatGPT/Codex 桌面端、CLI 和 IDE 中的 Codex 会话。准备足够空间存放 ZIP、临时文件和导入备份。

以下命令在 PowerShell 和 Git Bash 中都可用。若 `python` 指向旧版本，可以换成 `py -3.14`。

### 导出

在旧电脑上进入脚本所在目录，完全退出 Codex，再运行：

```text
python codex-project-conversation-transfer.py export
```

默认在脚本旁生成 `codex-chats.zip`。脚本会打印识别到的项目、项目根目录、会话及历史段数量。如果发现需要的历史段缺失，导出会停止。默认 ZIP 已存在时，请改用新的文件名。

```text
python codex-project-conversation-transfer.py export codex-chats-new.zip
```

### 导入

1. 把脚本和 ZIP 复制到新电脑。ZIP 可以直接读取。
2. 用新电脑自己的账号登录 Codex，初始化本地数据后完全退出。
3. 在脚本所在目录运行以下命令。使用自定义文件名时，在 `import` 后加上 ZIP 路径。

```text
python codex-project-conversation-transfer.py import
```

脚本会备份目标数据库、会话索引和侧栏登记，然后合并会话及项目归属。缺失的项目目录会按旧机的绝对路径创建为空目录。如果路径所在的盘符不可用或没有写权限，导入会停止。迁移保留原工作路径，建议把 Git 仓库放在两台机器相同的绝对路径上。

完成后打开 Codex，检查侧栏项目，打开一条旧会话，确认能翻到前文并继续发送消息。

### 重复导入

可以用新版脚本和同一个 ZIP 重跑，例如补齐旧版本遗漏的侧栏登记。重复导入会复用一致的会话文件和已有分页索引。输出 `0 new indexed chats` 表示这一轮没有新增聊天。另看 `project registrations` 和 `chat memberships` 的计数。

如果新机已经续聊，现有会话和包内历史可能不同。脚本遇到文件内容冲突会停止。保留新机的记录和导入备份。

### 帮助和预览

```text
python codex-project-conversation-transfer.py
python codex-project-conversation-transfer.py export --preview
python codex-project-conversation-transfer.py import codex-chats.zip --preview
```

导入预览会读取 ZIP 并检查目标状态，列出将创建的目录。正式导入时才写入项目目录和 Codex 数据。使用自定义 Codex 数据目录时，可以传入 `--codex-home`。

## English

Transfer local Codex project conversations between Windows computers. The script discovers projects automatically and bundles conversations, archived sessions, paginated history, and project assignments into a ZIP. Keep syncing your source code through Git.

### Requirements

- Windows and Python 3.14 or newer on both computers. The `compression.zstd` module is part of Python.
- Compatible Codex database schemas. Using the same Codex version on both computers is a good starting point.
- Quit Codex Desktop, the CLI, and any Codex IDE sessions before transferring. Allow space for the archive, temporary files, and destination backups.

The commands below work in PowerShell and Git Bash. Use `py -3.14` if `python` points to an older version.

### Export

On the source computer, open a terminal in the script directory and run:

```text
python codex-project-conversation-transfer.py export
```

The default archive is `codex-chats.zip` beside the script. Pass a different ZIP filename if the default already exists. Export stops if it detects missing history required by a selected conversation.

### Import

Copy the script and ZIP to the destination computer. Sign in to Codex with the destination account once, then quit Codex completely. From the script directory, run:

```text
python codex-project-conversation-transfer.py import
```

The script reads the ZIP directly, backs up destination databases and indexes, and merges conversations and project assignments. It creates empty directories for missing project roots at their original absolute paths. Put your Git repositories at the same paths to keep the restored chats' working directories usable. Open Codex afterward and verify that an old conversation shows its earlier messages and accepts a new one.

### Repeat an import

Run the same import command with the original ZIP to fill in project registrations and chat assignments, including after an earlier version imported the conversations. Identical conversation files and existing history index entries are reused. `0 new indexed chats` is expected if all conversations are already present. Check the `project registrations` and `chat memberships` counts. If the destination conversation has diverged, the script stops on the conflict.

### Help and preview

```text
python codex-project-conversation-transfer.py
python codex-project-conversation-transfer.py export --preview
python codex-project-conversation-transfer.py import codex-chats.zip --preview
```

Use `--codex-home` to select a custom Codex data directory. Import preview checks the package and destination and lists missing project directories before applying changes.

The ZIP contains private conversation content, tool output, and paths. Keep it private. Project files, sign-in credentials, app configuration, Skills, and plugins are excluded.

## 许可证 / License

本项目采用 MIT 许可证。 This project is licensed under the MIT License.

Copyright (c) 2026 Codex Project Conversation Transfer contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
