# -*- coding: utf-8 -*-
"""我们自己的 playwright 钩子 —— **抢占第三方钩子优先级**（本工程边界的一部分）

为什么必须自己写一个：

    `patchright`（playwright 的反检测改装分支）在装它的时候会带一个**叫做
    `hook-playwright.sync_api.py` 的 PyInstaller 钩子**，内容却是：

        from PyInstaller.utils.hooks import collect_data_files
        datas = collect_data_files("patchright")

    PyInstaller 按**模块名**找钩子，于是给 `playwright.sync_api` 收集数据时用上了它 →
    打进包的是**改装分支的 node 驱动**：Python 侧是正版 1.62.0，驱动侧却是 patchright 1.61.1。
    结果是「**未改装的浏览器**」这条边界承诺在打包后**静默失效**（模块名仍然叫 playwright，
    运行期的路径检查看不出来）。**注意**：`Analysis(excludes=[...])` 挡不住它——
    `excludes` 排除的是**模块图**，挡不住别人钩子里收的**数据文件**。

怎么修：PyInstaller 搜索钩子的顺序里，`hookspath`（本目录）**优先于**分发包自带的钩子。
所以在这里放一个同名的、只收**正版** playwright 驱动数据的钩子，就把它顶掉了。
`tools/build.py` 的产物核对还有一道**硬断言**兜底：包里必须有正版驱动、绝不许有分支目录。
"""

from PyInstaller.utils.hooks import collect_data_files

# 只收正版 playwright 的驱动数据（node 包 + browsers.json + LICENSE/NOTICE）
datas = collect_data_files("playwright")
