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
;  **许可页的三份文本也必须带 BOM**：`LicenseData` 对无 BOM 的文件按**本机 ANSI 代码页**
;  （简体中文机器上是 936/GBK）解码 —— 一份 UTF-8 无 BOM 的中文许可，到了安装向导里就是
;  满屏乱码生僻字（真实投诉）。三份文本由 packaging/make_installer_art.py 生成（带 BOM），
;  tools/build.py 在调 makensis 之前会**硬断言**每个文件的前 3 字节是 EF BB BF。
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
; 许可页内容**按界面语言选文件**（MUI2 官方做法：`LicenseLangString` + `$(MUILicense)`）。
; 为什么不是一份文件：三语共用一份中文文本 = 日/英用户看不懂（而 `LicenseData` 只吃一份文件）。
; 三份文本的来源与编码见文件开头；语言串本身定义在下面 `MUI_LANGUAGE` 之后（顺序有讲究）。
!insertmacro MUI_PAGE_LICENSE "$(MUILicense)"

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

; ── 许可文本的语言串（**必须放在 MUI_LANGUAGE 之后**，这一步实测过才敢这么写）──
; 顺序是死的：`MUI_PAGE_*` 必须在 `MUI_LANGUAGE` 之前，而 `${LANG_SIMPCHINESE}` 这类常量要等
; 语言表载入后才存在 → 唯一的合法顺序是：页面宏 → MUI_LANGUAGE → LicenseLangString。
; 顺序放错的症状（实测）：`${LANG_*}` 报 7025「not a valid language id, using 1033」，
; 三行全部塌到英语 1033，再报 6040「LangString 未在 ja/zh 语言表里设置」——**编译仍会成功**，
; 只有警告在提醒你：日/中文用户会看到英文。所以 `makensis` 的警告数必须为 0（见门禁）。
LicenseLangString MUILicense ${LANG_SIMPCHINESE} "art\license_zh.txt"
LicenseLangString MUILicense ${LANG_JAPANESE}    "art\license_ja.txt"
LicenseLangString MUILicense ${LANG_ENGLISH}     "art\license_en.txt"

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
    ; 许可文本：**三份都装**（每份都是 UTF-8 with BOM，见文件开头）。
    ; 为什么不分语言只装一份：安装目录可能被交接给别人、界面语言也可能事后改（marker 里的
    ; lang= 可以改），三份都在原地最不糊涂；三份加起来不到 20 KB，不值得为省这点体积加一个分支。
    ; 其中中文那份**是权威源**（由 docs/07-能力边界.md 生成），日/英是它的忠实翻译——
    ; 译文与原文并排放着，读者随时能自己核对。
    File /oname=LICENSE-zh.txt "art\license_zh.txt"
    File /oname=LICENSE-ja.txt "art\license_ja.txt"
    File /oname=LICENSE-en.txt "art\license_en.txt"

    ; 登记安装的凭据：卸载时靠它判断"这是我们装的"；**同时记下安装时选的语言**，
    ; 程序启动时读它决定界面语言（顺序：用户设置 > 这里 > 系统 UI 语言 > en-US）。
    ; 机主的硬要求：安装时选了简体中文，装完就绝不能蹦出日文——靠这一行兑现。
    FileOpen $0 "$INSTDIR\${APP_MARKER}" w
    FileWrite $0 "version=${APP_VERSION}$\r$\n"
    FileWrite $0 "installed=1$\r$\n"
    FileWrite $0 "exe=${APP_EXE}$\r$\n"
    FileWrite $0 "lang=$LANGUAGE$\r$\n"
    FileClose $0
    ; 快捷方式不在这里建：开始菜单那条（可取消勾选）在 SEC_SHORTCUTS，桌面那条（默认不勾）在
    ; SEC_DESKTOP，各自把「我建了什么」登记进 marker。共享资源的创建与登记放在一起，卸载才敢按登记删。

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
    ; 这一段**真的创建**那三个开始菜单入口。之前它是个空壳（`SectionIn RO` + 空段体）：永远勾选、
    ; 不可取消、什么都不做 —— 组件页上放着「勾了等于没勾」的条目，等于骗用户。
    ; 现在不写 `SectionIn RO`、也不写 `/o`：**默认勾选、但用户可以取消**（这才是组件页该有的语义）。
    ; 取消勾选后，卸载时那几个 Delete / RMDir 落在不存在的文件与目录上（空操作），不会误删别人。
    ; 上下文：**当前用户**（`$SMPROGRAMS` 的默认上下文）—— 与本次改动前一致，语义没换。
    CreateDirectory "$SMPROGRAMS\Daedalus"
    CreateShortcut "$SMPROGRAMS\Daedalus\${APP_NAME_ZH}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    CreateShortcut "$SMPROGRAMS\Daedalus\命令行采集（CLI）.lnk" "$INSTDIR\${APP_CLI}" "" "$INSTDIR\${APP_CLI}" 0
    CreateShortcut "$SMPROGRAMS\Daedalus\卸载 ${APP_NAME_ZH}.lnk" "$INSTDIR\un.${APP_NAME}.exe"
SectionEnd

Section /o "创建桌面快捷方式" SEC_DESKTOP
    ; `/o` = **默认不勾**：桌面是用户自己的地盘，本工程不静默动用户环境（与上面「可选：浏览器运行时」
    ; 同一条纪律）。想要图标的人自己勾。
    ;
    ; 上下文选 `all`（公共桌面 `C:\Users\Public\Desktop`），不是在当前用户上下文里建。理由：
    ;   本安装器是 `RequestExecutionLevel admin` 装到 Program Files（**机器级**安装），而 UAC 提权
    ;   完全可能用的是**另一个管理员账号**的凭据 —— 那时 `current` 指的是那个管理员，快捷方式会落在
    ;   **不是点安装的那个人**的桌面上，于是「勾了却没有图标」（这正是这次要修的投诉）。
    ;   写公共桌面则**任何用户（含发起安装的那位）都能看见**，勾选这件事才真的算数。
    ; 有意保留的不一致（本次不动的范围）：开始菜单那三个入口仍在 `current` 上下文 —— 换它的上下文
    ;   会牵动卸载侧的删除目标，属于另一台手术；这里只把**新增**的桌面图标做对。
    ; 卸载侧必须用同一个上下文（见 「un.程序文件」 段），否则删的是另一个位置。

    ; 登记在先、创建在后：万一后面创建失败，卸载时只会去删一个不存在的 .lnk（空操作）；
    ; 反过来（先创建后登记）一旦登记失败，公共桌面上就留一个**没人认领**的图标。
    ; ⚠️ 追加时必须 `FileSeek $0 0 END`：实测 NSIS 的 `FileOpen ... a` **不是**从文件末尾写，
    ;    而是从**偏移 0** 写（会直接覆盖 marker 头部）。marker 是卸载侧的凭据，毁了它等于卸载失灵。
    ClearErrors
    FileOpen $0 "$INSTDIR\${APP_MARKER}" a
    IfErrors desk_marker_fail
        FileSeek $0 0 END
        FileWrite $0 "desktop_shortcut=1$\r$\n"
        FileClose $0
        Goto desk_marker_done
    desk_marker_fail:
        DetailPrint "警告：没能把「桌面快捷方式」登记进 marker —— 卸载时将不会自动删除它。"
    desk_marker_done:

    SetShellVarContext all
    CreateShortCut "$DESKTOP\${APP_NAME_ZH}.lnk" "$INSTDIR\${APP_EXE}" "" "$INSTDIR\${APP_EXE}" 0
    SetShellVarContext current                  ; 立刻换回来，别影响后面的段
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_CORE} "程序本体（两个可执行文件 + 依赖 + 三份许可文本）。"
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_BROWSER} "浏览器运行时不随包分发；勾选只会记下意图并提示安装命令。"
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_SHORTCUTS} "开始菜单里放三个入口（程序 / CLI / 卸载）。取消勾选则不放。"
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_DESKTOP} "在公共桌面放一个图标（所有用户都能看到；默认不勾）。"
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
        ; 开始菜单（当前用户上下文）：不按 marker 里的开关判定 —— 我们发过的每一个版本都在这个位置
        ; 建过入口，若拿一个「新版本才会写」的开关去判，老安装升级上来的那批 .lnk 就永远没人删。
        ; 用户若在组件页取消勾选，这里删的是不存在的文件/目录（空操作），不会误伤。
        Delete "$SMPROGRAMS\Daedalus\${APP_NAME_ZH}.lnk"
        Delete "$SMPROGRAMS\Daedalus\命令行采集（CLI）.lnk"
        Delete "$SMPROGRAMS\Daedalus\卸载 ${APP_NAME_ZH}.lnk"
        RMDir "$SMPROGRAMS\Daedalus"          ; 只在目录空时才删 → 用户自己放的东西不会被连坐

        ; 桌面快捷方式（**公共**桌面）：**按 marker 条件化删除**（硬规矩 ③）。
        ; 桌面是所有用户共享的位置，无条件删等于替别人做决定；而「我到底建没建」只有安装时知道，
        ; 所以判据是安装时写下的 `desktop_shortcut=1`（见 SEC_DESKTOP）。
        ; 上下文必须与创建时**一致**（all）—— 否则删的是另一个位置，公共桌面的图标会留下来。
        StrCpy $2 0
        ClearErrors
        FileOpen $0 "$INSTDIR\${APP_MARKER}" r
        IfErrors un_desktop_done
        un_marker_loop:
            FileRead $0 $1
            StrCmp $1 "" un_marker_done
            ; 实测 `FileRead` **连行尾 CRLF 一起返回**，所以两种写法各认一次（手改过 marker 也认）
            StrCmp $1 "desktop_shortcut=1$\r$\n" 0 +2
                StrCpy $2 1
            StrCmp $1 "desktop_shortcut=1" 0 un_marker_loop
                StrCpy $2 1
            Goto un_marker_loop
        un_marker_done:
            FileClose $0
        un_desktop_done:
        StrCmp $2 1 0 un_no_desktop
            SetShellVarContext all
            Delete "$DESKTOP\${APP_NAME_ZH}.lnk"
            SetShellVarContext current        ; 立刻换回当前用户上下文（上面的开始菜单项用的是它）
        un_no_desktop:
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
