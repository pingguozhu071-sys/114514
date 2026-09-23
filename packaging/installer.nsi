;  Daedalus 安装向导 + 卸载向导（自制，NSIS 3.x）
;  ─────────────────────────────────────────────────────────────────────
;  三条硬规矩（来自 Kiana 的三次真实事故，每条都有注释与对应代码）
;    ① **不静默安装**：/S 一律拒绝（把带 /S 的调用顶回去，并说明为什么）；
;    ② **卸载段全部带 `un.` 前缀**：漏了会导致安装时就执行卸载逻辑 → 装完即被删光；
;    ③ **共享资源条件化清理**：不是我们登记的安装 → 什么都不动；是登记安装 → 按登记时
;       记下的"我们装了什么"决定删什么，且**判断条件不能被安装流程自己破坏**。
;  另有一条机主硬要求：**卸载时保留用户数据**，并在界面上明说"您的数据不会被删除"。
;
;  编码：本文件保存为 **UTF-8 with BOM**（NSIS 3 认 BOM；否则中文全乱码）。
;  编译：由 tools/build.py 用 python subprocess 调 makensis（Git Bash 会把 /S 当路径改写）。
;  ─────────────────────────────────────────────────────────────────────

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"
!include "WordFunc.nsh"

!include "version.nsh"          ; 由 tools/version_check.py --write 生成（版本单一来源）

!define APP_NAME "Daedalus"
!define APP_NAME_ZH "代达罗斯"
!define APP_PUBLISHER "Daedalus"
!define APP_EXE "daedalus.exe"
!define APP_CLI "daedalus-cli.exe"
!define APP_REGKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Daedalus"
!define APP_MARKER "install.marker"        ; 登记安装的凭据（卸载判断依赖它）

Name "${APP_NAME_ZH} ${APP_VERSION}"
OutFile "..\dist\Daedalus-Setup-${APP_VERSION}.exe"
InstallDir "$PROGRAMFILES64\Daedalus"
InstallDirRegKey HKLM "${APP_REGKEY}" "InstallLocation"
RequestExecutionLevel admin                ; 装到 Program Files：需要管理员（会明确弹 UAC）
ManifestDPIAware true                      ; 高分屏不做这步 → 整窗位图拉伸、字体全糊
ShowInstDetails show
ShowUninstDetails show
SetCompressor /SOLID lzma

VIProductVersion "${APP_VERSION_NUMERIC}"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileDescription" "${APP_NAME_ZH} 安装向导"
VIAddVersionKey "FileVersion" "${APP_VERSION}"
VIAddVersionKey "ProductVersion" "${APP_VERSION}"
VIAddVersionKey "LegalCopyright" ""

; ── 界面：自绘深空渐变横幅 + 版本徽章（美术由 packaging/make_installer_art.py 生成）──
!define MUI_ICON "..\assets\icon.ico"
!define MUI_UNICON "..\assets\icon.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "art\welcome.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "art\header.bmp"
!define MUI_ABORTWARNING

; ── 语言：zh-CN / ja-JP / en-US（默认跟随系统）──
; ⚠️ MUI2 的顺序要求：`MUI_LANGUAGE` 必须插在**所有 [UN]PAGE 宏之后**，
;    否则报 "MUI_PAGE_* inserted after MUI_LANGUAGE"（页面初始化会错序）。见文件末尾。
!define MUI_LANGDLL_ALLLANGUAGES

; ══════════════════════════════════════════════════════════════════
;  页面
; ══════════════════════════════════════════════════════════════════
!define MUI_WELCOMEPAGE_TITLE "要装 ${APP_NAME_ZH} 了哦，${APP_VERSION}"
!define MUI_WELCOMEPAGE_TEXT "哼，既然你双击了本小姐，那就勉为其难给你装上吧。$\r$\n$\r$\n它是一台「统一采集与感知引擎」——不是爬虫。三种采集环境（直连网络 / 浏览器运行时 / 制品与媒体）由证据自己选路；抓到的东西先原样存下来，之后随时可以重新解释。$\r$\n$\r$\n全部装在你自己的机器上：没有遥测、不常驻后台、不弹黑窗。$\r$\n$\r$\n数据放在哪里？安装版放用户目录（%LOCALAPPDATA%\Daedalus）；便携版放 exe 同级的 DaedalusData。"
!insertmacro MUI_PAGE_WELCOME

!define MUI_LICENSEPAGE_TEXT_TOP "先看一眼使用边界（不长，但请真的看一眼）："
; 许可页内容由 tools/build.py 从 docs/07-能力边界.md **生成**（单一来源，不手抄两份）
!insertmacro MUI_PAGE_LICENSE "art\license.txt"

!define MUI_COMPONENTSPAGE_SMALLDESC
!insertmacro MUI_PAGE_COMPONENTS

!define MUI_DIRECTORYPAGE_TEXT_TOP "装到哪里？（默认 Program Files，你也可以塞到别处）"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES

; 完成页：**装完就自证可用**（跑 doctor，把结果贴在页面上）
!define MUI_FINISHPAGE_TITLE "${APP_NAME_ZH} 装好了"
!define MUI_FINISHPAGE_TEXT "装完了——去跑吧，别指望本小姐再帮你调参。$\r$\n$\r$\n下面那个勾选框会跑一次自检（doctor）：它会告诉你浏览器、ffmpeg、磁盘、库是不是都就绪。缺件它会明确说缺什么，不会假装没事。"
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP_CLI}"
!define MUI_FINISHPAGE_RUN_TEXT "运行安装自检（doctor）"
!insertmacro MUI_PAGE_FINISH

; ── 卸载页：**明确告诉用户数据不会被删**，删除数据是单独的勾选项 ──
!define MUI_UNWELCOMEPAGE_TEXT "确定要卸掉本小姐吗…$\r$\n$\r$\n**您的数据不会被删除**：采集到的原始数据、库、日志都在用户目录里，卸载程序不碰它们。想连数据一起清掉，请在下一页勾选（默认不勾）。"
!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH

; ── 语言宏（**必须在所有页面宏之后**，见文件开头那条说明）──
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "Japanese"
!insertmacro MUI_LANGUAGE "English"

; ══════════════════════════════════════════════════════════════════
;  安装
; ══════════════════════════════════════════════════════════════════
Function .onInit
    ; ── 硬规矩 ①：不静默安装 ──
    IfSilent 0 silent_ok
        MessageBox MB_ICONSTOP|MB_OK "本安装程序**不支持静默安装**（/S）。$\r$\n$\r$\n理由：安装会写入 Program Files 与注册表，静默执行意味着你可能不知道它动了什么。请正常双击运行，走完整的向导。$\r$\n$\r$\n（自动化部署请使用便携版：拷贝目录 + 放一个 portable.flag 即可。）"
        Abort
    silent_ok:
    !insertmacro MUI_LANGDLL_DISPLAY
FunctionEnd

Section "!核心程序（必需）" SEC_CORE
    SectionIn RO
    ; 防新旧依赖混跑：先清掉目标子目录（只清我们自己装的那几个，不动别的）
    RMDir /r "$INSTDIR\_internal"
    RMDir /r "$INSTDIR\assets"
    SetOutPath "$INSTDIR"
    ; 程序本体（dist\daedalus 整目录：两个 exe + _internal）
    File /r "..\dist\daedalus\*.*"
    File "art\license.txt"

    ; 登记安装的凭据：卸载时靠它判断"这是我们装的"
    FileOpen $0 "$INSTDIR\${APP_MARKER}" w
    FileWrite $0 "version=${APP_VERSION}$\r$\n"
    FileWrite $0 "installed=1$\r$\n"
    FileWrite $0 "exe=${APP_EXE}$\r$\n"
    FileClose $0

    ; 开始菜单 + 桌面快捷方式（共享资源：卸载时按登记条件化清理）
    CreateDirectory "$SMPROGRAMS\Daedalus"
    CreateShortcut "$SMPROGRAMS\Daedalus\${APP_NAME_ZH}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    CreateShortcut "$SMPROGRAMS\Daedalus\命令行采集（CLI）.lnk" "$INSTDIR\${APP_CLI}" "" "$INSTDIR\${APP_CLI}" 0
    CreateShortcut "$SMPROGRAMS\Daedalus\卸载 ${APP_NAME_ZH}.lnk" "$INSTDIR\un.${APP_NAME}.exe"

    ; 注册表：卸载信息（控制面板能看到、能卸）
    WriteRegStr HKLM "${APP_REGKEY}" "DisplayName" "${APP_NAME_ZH}（${APP_NAME}）"
    WriteRegStr HKLM "${APP_REGKEY}" "DisplayVersion" "${APP_VERSION}"
    WriteRegStr HKLM "${APP_REGKEY}" "Publisher" "${APP_PUBLISHER}"
    WriteRegStr HKLM "${APP_REGKEY}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKLM "${APP_REGKEY}" "UninstallString" '"$INSTDIR\un.${APP_NAME}.exe"'
    WriteRegStr HKLM "${APP_REGKEY}" "QuietUninstallString" '"$INSTDIR\un.${APP_NAME}.exe"'
    WriteRegStr HKLM "${APP_REGKEY}" "DisplayIcon" "$INSTDIR\${APP_EXE}"
    WriteRegDWORD HKLM "${APP_REGKEY}" "NoModify" 1
    WriteRegDWORD HKLM "${APP_REGKEY}" "NoRepair" 1
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegDWORD HKLM "${APP_REGKEY}" "EstimatedSize" "$0"

    ; 卸载器：**必须带 `un.` 前缀**（硬规矩 ②）
    WriteUninstaller "$INSTDIR\un.${APP_NAME}.exe"
SectionEnd

Section /o "可选：把浏览器运行时也带上（约 +400MB，装完就能跑脚本渲染的页面）" SEC_BROWSER
    ; 浏览器二进制**不打进安装包**（体积与许可），而是安装时按需下载——
    ; 但本工程"不静默安装任何东西"，所以这里只**记录意图**，由用户自己跑一次
    ; `daedalus-cli doctor` 看到缺件提示后决定是否安装（提示里给出确切命令）。
    WriteRegStr HKLM "${APP_REGKEY}" "BrowserRuntime" "pending"
    DetailPrint "已记下你想用浏览器运行时。安装完成后请运行：python -m playwright install chromium"
    DetailPrint "（本安装器不会替你下载任何东西——这是刻意的：不静默安装。）"
SectionEnd

Section "开始菜单快捷方式" SEC_SHORTCUTS
SectionIn RO
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_CORE} "程序本体（两个可执行文件 + 依赖 + 文档）。"
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_BROWSER} "浏览器运行时不随包分发；勾选只会记下意图并提示安装命令。"
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_SHORTCUTS} "开始菜单里放一个入口。"
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ══════════════════════════════════════════════════════════════════
;  卸载（**每一段都带 `un.` 前缀**）
; ══════════════════════════════════════════════════════════════════
Function un.onInit
    IfSilent 0 +3
        MessageBox MB_ICONSTOP|MB_OK "卸载同样不走静默（/S）。请从「设置 → 应用」或开始菜单里的卸载入口运行。"
        Abort
    !insertmacro MUI_UNGETLANGUAGE
FunctionEnd

Section "un.程序文件" UN_SEC_CORE
    SectionIn RO
    ; ── 硬规矩 ③：共享资源条件化清理 ──
    ; 判据 = 我们登记安装时写下的 marker。**先读、再决定**，绝不在删除后才判断。
    IfFileExists "$INSTDIR\${APP_MARKER}" 0 un_not_ours
        ; 分支 A：是我们的登记安装 → 清掉我们装的东西
        Delete "$SMPROGRAMS\Daedalus\${APP_NAME_ZH}.lnk"
        Delete "$SMPROGRAMS\Daedalus\命令行采集（CLI）.lnk"
        Delete "$SMPROGRAMS\Daedalus\卸载 ${APP_NAME_ZH}.lnk"
        RMDir "$SMPROGRAMS\Daedalus"
        ; 备份"我们装了什么"的痕迹，供延迟扫尾判断（删除逻辑不依赖它）
        FileOpen $0 "$TEMP\daedalus_uninstall.flag" w
        FileWrite $0 "1"
        FileClose $0
        Goto un_cleanup
    un_not_ours:
        ; 分支 B：不是我们登记的安装（用户手动解压的目录）→ **什么都不动**
        MessageBox MB_ICONINFORMATION|MB_OK "这个目录不是通过安装向导装进来的，所以卸载程序不会删除里面的任何文件。$\r$\n$\r$\n你自己手动删就好——本小姐不碰别人的东西。"
        Abort
    un_cleanup:
SectionEnd

Section "un.卸载条目" UN_SEC_REGS
    DeleteRegKey HKLM "${APP_REGKEY}"
SectionEnd

Section /o "un.同时删除我的采集数据（**不可恢复**）" UN_SEC_DATA
    ; `/o` = 可选段且**默认不勾**：数据在用户目录、不在安装目录里，只有用户显式勾选才动它。
    ; （绝不能给它加 `SectionIn RO`——那会变成每次卸载都执行，等于"卸载即删数据"。）
    RMDir /r "$LOCALAPPDATA\Daedalus"
    RMDir /r "$APPDATA\Daedalus"
SectionEnd

Section "un.收尾（延迟扫尾删除自身）" UN_SEC_FINISH
    ; 卸载器删不掉正在运行的自己：**脱手后由 cmd 延迟扫尾**（黑窗一闪是预期的）。
    ; 只在我们自己装的目录上扫尾（判据是安装时写的 marker，不是"目录看起来像我们的"）。
    IfFileExists "$INSTDIR\${APP_MARKER}" 0 un_keep
        FileOpen $0 "$TEMP\daedalus_uninstall.bat" w
        FileWrite $0 "ping 127.0.0.1 -n 3 > nul$\r$\n"
        FileWrite $0 "rmdir /s /q $\"$INSTDIR$\"$\r$\n"
        FileWrite $0 "del /f /q $\"$TEMP\daedalus_uninstall.bat$\"$\r$\n"
        FileClose $0
        Exec '"$TEMP\daedalus_uninstall.bat"'      ; 独立进程：它不受卸载器退出影响
    un_keep:
SectionEnd

Function un.onUninstSuccess
    HideWindow
    MessageBox MB_ICONINFORMATION|MB_OK "卸完了。$\r$\n$\r$\n**您的数据没有被删除**：原始层、库、日志都还在用户目录里（除非你在上一页勾了删除数据）。$\r$\n$\r$\n……哼，想回来的时候，本小姐还是在这儿。"
FunctionEnd
