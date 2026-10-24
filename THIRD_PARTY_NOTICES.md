# 第三方组件与许可证

本仓库自行编写的应用代码按根目录 `LICENSE` 中的 **MIT** 条款提供。MIT 不重新授权 Qt、Python、密码学库、构建工具或用户导入的第三方技能。发布包中实际收集的许可证全文及版本清单也属于交付内容，应随二进制一起保留。

下表列出主要直接依赖及发布工具，具体安装版本以对应发布包的清单为准；Qt/PySide 当前构建路线为 6.8 系列。表中的上游源码链接便于定位，应在重新构建时选取与所发二进制相同的 tag/版本。

| 组件 | 使用方式与许可证 | 官方说明 / 源码 |
| --- | --- | --- |
| Python / CPython | 便携包中的解释器运行时；PSF License 及其附带许可 | [Python License](https://docs.python.org/3/license.html)、[CPython source](https://github.com/python/cpython) |
| Tcl/Tk | 完整安装器的 Tkinter 界面运行时；Tcl/Tk 随带的宽松许可证，随安装目录 licenses 提供 | [Tcl/Tk 源码](https://github.com/tcltk/tk) |
| PySide6 / Shiboken6 | Qt for Python 动态绑定；本项目采用其开源 LGPLv3 路线，部分内容可能有独立许可 | [Qt for Python 6.8 licenses](https://doc.qt.io/qtforpython-6.8/licenses.html)、[Pyside source](https://code.qt.io/cgit/pyside/pyside-setup.git/) |
| Qt 6.8 动态库及插件 | 使用的 Qt 模块按相应 LGPLv3/其他上游许可分发；本应用代码的 MIT 不覆盖这些二进制 | [Qt licensing](https://doc.qt.io/qt-6/licensing.html)、[Qt 6.8 source archives](https://download.qt.io/archive/qt/6.8/)、[QtBase source](https://code.qt.io/cgit/qt/qtbase.git/) |
| cryptography | 加密及密钥派生；Apache License 2.0 或 BSD 双许可，依其分发包授权 | [cryptography licenses](https://github.com/pyca/cryptography/blob/main/LICENSE)、[source](https://github.com/pyca/cryptography) |
| requests | HTTP 客户端；Apache License 2.0 | [license/source](https://github.com/psf/requests/blob/main/LICENSE) |
| PyYAML | YAML 头部解析；MIT | [license/source](https://github.com/yaml/pyyaml/blob/main/LICENSE) |
| PyInstaller | 打包工具；GPL，附带允许分发所构建应用的 bootloader exception | [官方 license 与例外](https://pyinstaller.org/en/stable/license.html)、[source](https://github.com/pyinstaller/pyinstaller) |

requests 的依赖（例如 urllib3、certifi、charset-normalizer、idna）、cryptography 随带的底层加密组件，以及 Qt 随带第三方资源可能有自己的 MIT、BSD、Apache、MPL 或其他许可。不要把它们统一标成 MIT。发布时应以实际收集到的版本许可证全文为准；未使用或未随包的库不因出现在开发机上而自动算作发布依赖。

## Qt / PySide 动态库的分发说明

便携版本采用依赖目录形式分发 Qt/PySide 动态库，不把它们的授权改为 MIT。请保留随包的 LGPLv3/GPLv3 及相关许可、版权信息和本说明。使用者可按相应许可证研究和修改这些开源组件，在相容接口与 ABI 的前提下以自己的构建替换动态库；本项目不会以自己的 MIT 条款禁止许可证允许的调试或为修改库而进行的逆向工程。

所使用的 LGPL 组件源码可从上述 Qt 官方仓库和归档获取，须匹配发布清单中的具体版本；重新发布修改过的 Qt/PySide 时，发布者还应提供匹配的修改和构建所需材料。仅提供一个泛泛的主页不能替代实际版本核对和适用的源码分发义务。当前应用没有自行修改 Qt/PySide 上游库的计划；若实际构建发生变化，应同步更新此说明。

本项目只使用构建时明确需要的 Qt 模块。维护者增加新的 Qt 模块，尤其是采用不同许可的模块时，需要重新核对分发条款。可参照 [Qt 开源 LGPL 使用说明](https://www.qt.io/licensing/open-source-lgpl-obligations)与[Qt for Python 商业/开源说明](https://doc.qt.io/qtforpython-6.8/commercial/index.html)。

## 功能调研项目

[TraeTools](https://github.com/star620/TraeTools)、[Trae-Work-CN-Account-Manager](https://github.com/xhrxgr/Trae-Work-CN-Account-Manager)、[cockpit-tools](https://github.com/jlcodes99/cockpit-tools)、[skills-manager](https://github.com/sciman-top/skills-manager)与[AT-Switch](https://github.com/atswitch/at-switch)作为公开功能调研来源，未将其实现或资产复制进本交付。它们不是本应用的打包依赖。各项目的授权和范围对应见[功能文档](docs/FEATURES.md)。

TRAE CN 本体及其商标、官方文档、登录服务和奖励规则属于相应权利人。本项目不分发 TRAE 本体，且与官方没有隶属关系。用户导入的 Skill、脚本、模板或其他资源仍按其作者许可处理；使用本管理器导入不会使其自动变成 MIT，也不授予再次公开上传的权利。
