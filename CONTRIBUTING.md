# 维护与发布

## 工具名和读者入口

公开工具沿用 `dl-领域-功能` 的命名，例如 `dl-benshi-interview`。每个工具放在 `skills/` 的同名目录，同时登记到[插件清单](.claude-plugin/marketplace.json)。

工具的用途、运行环境或用法发生变化时，沿着读者的路径走一遍。

1. [首页](README.md)能不能用一句话告诉读者这个工具帮什么忙，并把读者带到对应的使用教程？
2. [安装指南](docs/安装指南.md)中的下载入口、支持的工具、开始第一轮的指令还适用吗？
3. 单个工具的 `README.md` 是否说明了要准备什么、第一句话怎么说、怎样看结果？下载 ZIP 的人只能看到包内文件，包外引用请使用能公开访问的链接。
4. 新装后按教程试一次。需要付费或操作真实账号的步骤要标出前置条件，用示例数据跑过的部分写清楚。

## 版本与安装包

根目录的 [VERSION](VERSION) 记录合集版本。要更新正式安装包，选用下一个正式版本号，用 `python3 tools/set_version.py X.Y.Z` 同步清单和各工具已有的版本文件，再用 `python3 tools/check_versions.py` 检查。

打包器使用 Git 已跟踪的文件。公共教程要随代码提交，客户名单、密钥和运行记录留在本地。小红书私信跟进的安装包按[公开文件清单](tools/build-xhs-release.py)构建，并附校验和及文件清单。

提交标题写清具体改动。主分支的 [Test Skills](.github/workflows/test.yml) 通过后，在同一个提交上打 `vX.Y.Z` 标签并推送。[Release Skills](.github/workflows/release.yml) 会生成 GitHub Release 和每个工具的 ZIP。发布页写清本次改动，打开线上首页，下载正式包，确认包内教程和启动入口与本次提交一致。
