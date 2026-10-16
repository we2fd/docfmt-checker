"""把「格致 · 学术文档格式智能检查与规范化助手」打包成 Windows 免安装便携版。

用法：
    python scripts/build_portable.py                 # 输出到 F:/wbwork/docfmt-portable
    python scripts/build_portable.py --out D:/xx     # 指定输出目录
    python scripts/build_portable.py --clean         # 先删掉旧输出再重建
    python scripts/build_portable.py --no-zip        # 只生成目录，不打 zip

目标机器**不需要装 Python、不需要联网**：双击 START.bat 即可。

打包思路（为什么这么做，都踩过）：
  1. 内嵌官方 **embeddable Python**，与系统 Python 完全隔离，卸载只需删目录；
  2. 依赖**按真实闭包精确拷贝**，不是把整个 site-packages 搬过去 ——
     本机 venv 里还有另一个项目的重依赖（chroma / numpy 等），全搬会白白多出上百 MB；
  3. `pythonXXX._pth` 必须显式加上 `Lib\\site-packages` 与 `import site`，
     否则 embed 版的隔离模式会让第三方包一律 import 不到；
  4. 依赖直接从现有 venv 拷，不重新 pip 下载 —— wheel 本身就是 win_amd64 + cp3xx 的预编译包。
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT.parent / "docfmt-portable"
CACHE_DIR = PROJECT_ROOT / ".build-cache"

PY_VERSION = "3.13.14"
EMBED_NAME = f"python-{PY_VERSION}-embed-amd64.zip"
EMBED_URLS = [
    f"https://mirrors.huaweicloud.com/python/{PY_VERSION}/{EMBED_NAME}",
    f"https://registry.npmmirror.com/-/binary/python/{PY_VERSION}/{EMBED_NAME}",
    f"https://www.python.org/ftp/python/{PY_VERSION}/{EMBED_NAME}",
]

# 运行时真正需要的发行包。
# ⚠️ python-multipart 不在 fastapi 的依赖元数据里（FastAPI 只在解析上传表单时才 import 它），
#    但缺了它上传接口会直接 500 —— 这类「隐式运行时依赖」是打包最容易漏的东西。
# 💡 httpx 是被 scripts/selfcheck.py 用到的，带上它才能让用户在目标机器上自检。
ROOT_DISTS = [
    "fastapi",
    "uvicorn",
    "python-docx",
    "pyyaml",
    "pydantic",
    "httpx",
    "python-multipart",
]

# 随包分发的内容（相对工程根）
BUSINESS_ITEMS = [
    "backend",
    "frontend",
    "samples",
    "scripts",
    "docs",
    "run.py",
    "README.md",
    "CHANGELOG.md",
    "requirements.txt",
]

# 不分发：开发期工具与产物
EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", "artifacts", ".git", "data", ".build-cache"}
EXCLUDED_REL_FILES = {
    "scripts/build_portable.py",   # 打包脚本自身
    "backend/pytest.ini",          # 便携包里不带 tests/，留它只会指向不存在的目录
    ".gitignore",
}
EXCLUDED_REL_DIRS = {
    "backend/tests",               # 跑测试需要 pytest，便携包里不带
}


def log(message: str) -> None:
    print(message, flush=True)


def norm(name: str) -> str:
    return name.strip().lower().replace("_", "-")


# --------------------------------------------------------------- 依赖闭包

def resolve_closure(roots: Iterable[str]) -> dict[str, str]:
    """算出 roots 的传递依赖闭包，返回 {规范化包名: 版本}。"""
    from packaging.requirements import Requirement

    installed: dict[str, str] = {}
    for dist in metadata.distributions():
        name = (dist.metadata.get("Name") or "").strip()
        if name:
            installed[norm(name)] = dist.version

    closure: dict[str, str] = {}
    queue = [norm(item) for item in roots]
    while queue:
        key = queue.pop()
        if key in closure or key not in installed:
            continue
        closure[key] = installed[key]
        for raw in metadata.requires(key) or []:
            try:
                req = Requirement(raw)
            except Exception:
                continue
            if req.marker is not None:
                try:
                    if not req.marker.evaluate():
                        continue
                except Exception:
                    pass
            queue.append(norm(req.name))
    return closure


def copy_dist(dist_name: str, site_packages: Path) -> int:
    """按发行包自带的文件清单精确拷贝，返回拷贝的文件数。"""
    dist = metadata.distribution(dist_name)
    copied = 0
    for entry in dist.files or []:
        rel = str(entry).replace("\\", "/")
        parts = rel.split("/")
        if ".." in parts:
            continue  # 指向 site-packages 之外（如 Scripts/*.exe），不需要
        if "__pycache__" in parts or rel.endswith(".pyc"):
            continue
        source = Path(dist.locate_file(entry))
        if not source.is_file():
            continue
        target = site_packages / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    return copied


# --------------------------------------------------------------- 内嵌运行时

def download_embed(dest: Path) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if dest.exists() and zipfile.is_zipfile(dest):
        log(f"  复用已缓存的内嵌运行时：{dest.name}（{dest.stat().st_size // 1024} KB）")
        return dest
    last_error = ""
    for url in EMBED_URLS:
        log(f"  下载 {url}")
        try:
            with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as handle:
                shutil.copyfileobj(response, handle)
            if zipfile.is_zipfile(dest):
                log(f"  完成（{dest.stat().st_size // 1024} KB）")
                return dest
            last_error = "下载到的不是 zip（可能是错误页）"
            dest.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            log(f"  失败：{exc}")
    raise SystemExit(f"所有镜像都下载失败（{last_error}），请手工下载 {EMBED_NAME} 放到 {CACHE_DIR}")


def setup_runtime(package_root: Path) -> None:
    python_dir = package_root / "python"
    if python_dir.exists():
        shutil.rmtree(python_dir)
    python_dir.mkdir(parents=True, exist_ok=True)

    archive = download_embed(CACHE_DIR / EMBED_NAME)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(python_dir)
    log("  已解压内嵌 Python")

    major, minor = PY_VERSION.split(".")[:2]
    stdlib_zip = f"python{major}{minor}.zip"
    pth_files = sorted(python_dir.glob("*._pth"))
    if not pth_files:
        raise SystemExit("内嵌运行时里没有找到 ._pth 文件，无法配置隔离模式")
    pth = pth_files[0]
    # 顺序与内容都不能改：embed 版默认不加载 site，第三方包一律 import 不到。
    # `import site` 前面不能有注释符号。
    pth.write_text(
        "\n".join([stdlib_zip, ".", "Lib\\site-packages", "import site"]) + "\n",
        encoding="utf-8",
    )
    log(f"  已配置 {pth.name}（含 Lib\\site-packages 与 import site）")

    (python_dir / "Lib" / "site-packages").mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------- 业务内容

def _ignore(dir_path: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    base = Path(dir_path)
    for name in names:
        if name in EXCLUDED_DIR_NAMES:
            ignored.add(name)
            continue
        try:
            rel = (base / name).relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            continue
        if rel in EXCLUDED_REL_FILES or rel in EXCLUDED_REL_DIRS:
            ignored.add(name)
    return ignored


def copy_business(package_root: Path) -> None:
    for item in BUSINESS_ITEMS:
        source = PROJECT_ROOT / item
        if not source.exists():
            log(f"  跳过（不存在）：{item}")
            continue
        target = package_root / item
        if source.is_dir():
            shutil.copytree(source, target, ignore=_ignore, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)
    log(f"  已拷贝 {len(BUSINESS_ITEMS)} 项业务内容")


# --------------------------------------------------------------- 启动入口

START_BAT = """@echo off
REM ============================================================
REM  DocFmtChecker - portable launcher
REM  Double-click this file. No Python installation required.
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

if not exist "%~dp0python\\python.exe" (
    echo.
    echo [ERROR] "python\\python.exe" not found.
    echo         The python folder is missing or incomplete.
    echo         Please copy the WHOLE folder, not just a few files.
    echo.
    pause
    exit /b 1
)

echo Starting DocFmtChecker, please wait ...
echo.
"%~dp0python\\python.exe" "%~dp0run.py" %*

echo.
echo Service stopped.
pause
endlocal
"""

SELFCHECK_BAT = """@echo off
REM ============================================================
REM  DocFmtChecker - end-to-end self check
REM  Verifies this copied folder really works on THIS machine.
REM ============================================================
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set PYTHONUNBUFFERED=1

if not exist "%~dp0python\\python.exe" (
    echo [ERROR] "python\\python.exe" not found. Copy the WHOLE folder.
    pause
    exit /b 1
)

"%~dp0python\\python.exe" "%~dp0scripts\\selfcheck.py" %*

echo.
pause
endlocal
"""


def write_launchers(package_root: Path) -> None:
    # .bat 内容保持全 ASCII：中文提示交给被调用的 Python 脚本打印，避免代码页乱码。
    for name, content in (("START.bat", START_BAT), ("自检.bat", SELFCHECK_BAT)):
        path = package_root / name
        with open(path, "w", encoding="ascii", newline="\r\n") as handle:
            handle.write(content)
    log("  已生成 START.bat 与 自检.bat")


def write_readme(package_root: Path) -> None:
    from datetime import datetime

    text = f"""格致 · 学术文档格式智能检查与规范化助手
便携版（免安装）　打包日期：{datetime.now().strftime('%Y-%m-%d')}

============================================================
 怎么用（三步）
============================================================

1. 把**整个文件夹**拷到 U 盘（不要只拷其中几个文件，python 目录不能少）；
2. 在目标电脑上，双击 **START.bat**；
3. 等几秒，浏览器会自动打开。如果没自动打开，手动访问窗口里显示的地址
   （形如 http://127.0.0.1:8300/ ）。

关闭那个黑色命令行窗口，就是停止服务。

============================================================
 环境要求
============================================================

* **不需要**安装 Python
* **不需要**联网
* **不需要**数据库
* 支持系统：64 位 Windows 10 / 11
  （Windows 7 不支持 —— 内嵌的 Python 3.13 需要较新的系统组件）
* 浏览器：Chrome / Edge / Firefox 等现代浏览器（推荐 Chrome 或 Edge）

============================================================
 三十秒看效果
============================================================

网页左侧有一栏「一键载入演示样例」，里面预置了 4 份 Word 论文：

  01-规范样本.docx          体检分 100（完全合规，用来证明不误报）
  02-排版问题样本.docx      体检分 46
  03-结构问题样本.docx      体检分 71
  04-一致性与标点样本.docx  体检分 66

点任意一份，会自动完成上传、检查，右侧立刻出现格式体检报告。
点报告里任意一条问题的「定位到原文」，会跳到原文并高亮该段。

============================================================
 常见问题
============================================================

Q: 双击 START.bat 后窗口一闪就没了？
A: 说明运行出错。请在该目录按住 Shift 右键 → 「在此处打开命令窗口」，
   然后手工输入 START.bat 回车，这样能看到报错信息。

Q: 浏览器显示「无法访问此网站」？
A: 可能是服务还在启动（低配机器要 5～10 秒），稍等再刷新。
   也可能是端口被占用，启动窗口里会显示实际使用的端口，照着访问即可。

Q: 提示无法识别的文件类型？
A: 本工具只支持 .docx（Word 2007 及以上）。
   旧版 .doc 请先用 Word 打开 → 「另存为」→ 文件类型选「Word 文档 (*.docx)」。

Q: 提示端口被占用？
A: 会自动顺延到下一个可用端口，启动窗口里会打印实际地址，按它访问就行。

Q: 想确认这个包在别人的电脑上真的能用？
A: 双击 **自检.bat**，它会自己起服务跑一遍完整流程并给出报告。
   没装 Chrome/Node 时，相关渲染检查会标记为「跳过」而不是失败。

Q: 我的论文数据会被上传到网上吗？
A: 不会。所有数据都保存在本目录的 data 文件夹里，程序全程不联网。

Q: 我们学校的格式要求和内置的不一样，怎么办？
A: 不用改代码。打开「规则集」页签，可以
   ①「快速调整」逐项改阈值；或
   ②「参考模板校准」直接上传一份你们学校的模板 .docx，让它反推；
   如果模板本身格式也不统一，推导结果会标黄提示你人工复核。

============================================================
 目录说明
============================================================

  START.bat        双击启动（主入口）
  自检.bat         端到端自检
  run.py           启动器：挑空闲端口 → 起服务 → 自动开浏览器
  python\\          内嵌 Python 运行时（含全部依赖，不要删）
  backend\\         后端：接口层 + 核心检查引擎（六模块）
  backend\\rules\\default.yaml   56 条格式规则（可自行修改）
  frontend\\        前端页面（Vue 3 + Element Plus，已本地化）
  samples\\         4 份演示样例 .docx
  scripts\\         生成样例 / 端到端自检
  docs\\            设计文档：架构设计、规则设计、用户手册、测试用例、答辩要点
  data\\            运行后自动创建：上传的文档、修复产物、参考模板、SQLite 数据库

============================================================
 想改格式规则？
============================================================

内置的 56 条规则是一套**本科论文通用口径**，不代表任何学校。要按你们学校的
要求检查，三条路（都不用改代码）：

1. 「规则集 → 快速调整」：把字号、行距、页边距、缩进、字数区间这些**靠数字判定**
   的项摊成表单，改一个数字即全库生效，每项都标了单位和合理范围；
2. 「规则集 → 参考模板校准」：**上传一份你们学校的模板 .docx**，
   它会逐条量出模板的实际格式并反推出建议阈值，你勾选后点「应用」才生效；
3. 「规则集 → YAML 原文」：直接改配置文件，适合批量调整。

改完点「恢复内置默认」可以一键复位。
"""

    # 记事本打开中文说明必须带 BOM，否则乱码
    (package_root / "使用说明.txt").write_text(text, encoding="utf-8-sig")
    log("  已生成 使用说明.txt（utf-8-sig）")


# --------------------------------------------------------------- 主流程

def make_zip(package_root: Path) -> Path:
    archive = shutil.make_archive(
        str(package_root), "zip", root_dir=str(package_root.parent), base_dir=package_root.name
    )
    return Path(archive)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DocFmtChecker 免安装便携版")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"输出目录，默认 {DEFAULT_OUT}")
    parser.add_argument("--clean", action="store_true", help="先删除已存在的输出目录")
    parser.add_argument("--no-zip", action="store_true", help="不生成 zip")
    args = parser.parse_args()

    package_root = Path(args.out).resolve()

    log("=" * 62)
    log("  格致 · DocFmtChecker —— 便携版打包")
    log("=" * 62)
    log(f"输出目录：{package_root}")

    if package_root.exists():
        if not args.clean:
            log("输出目录已存在。加 --clean 可先删除重建。")
            return 1
        log("清理旧输出……")
        shutil.rmtree(package_root)
    package_root.mkdir(parents=True, exist_ok=True)

    log("\n[1/5] 准备内嵌 Python 运行时")
    setup_runtime(package_root)

    log("\n[2/5] 解析依赖闭包")
    closure = resolve_closure(ROOT_DISTS)
    for name in sorted(closure):
        log(f"    {name}=={closure[name]}")

    log("\n[3/5] 拷贝依赖文件")
    site_packages = package_root / "python" / "Lib" / "site-packages"
    total_files = 0
    for name in sorted(closure):
        total_files += copy_dist(name, site_packages)
    log(f"  共 {len(closure)} 个发行包 / {total_files} 个文件")

    log("\n[4/5] 拷贝业务内容并生成启动文件")
    copy_business(package_root)
    write_launchers(package_root)
    write_readme(package_root)

    log("\n[5/5] 统计体积")
    total = sum(f.stat().st_size for f in package_root.rglob("*") if f.is_file())
    log(f"  目录大小：{total / 1024 / 1024:.1f} MB")

    if args.no_zip:
        log("\n完成（未打 zip）。")
    else:
        log("\n正在压缩……")
        archive = make_zip(package_root)
        log(f"  {archive}（{archive.stat().st_size / 1024 / 1024:.1f} MB）")
        log("\n完成。把整个文件夹（或那个 zip）拷进 U 盘即可。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
