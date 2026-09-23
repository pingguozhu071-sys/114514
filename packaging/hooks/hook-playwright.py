# -*- coding: utf-8 -*-
"""`hook-playwright.py`：与 `hook-playwright.sync_api.py` 同一件事（见那个文件的说明）。

有些打包路径会按顶层包名 `playwright` 找钩子，所以两个名字都放一份——
少一个就可能让第三方同名钩子再次生效（那正是我们要挡住的）。
"""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("playwright")
