"""插件截图接入规范检查；这是静态代码规范，不是任意 Python 的执行沙箱。"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any


# 浏览器由平台独立部署，插件不得导入自动化包或浏览器安装器自行建立另一条路径。
_BROWSER_MODULES = frozenset({
    "playwright", "selenium", "seleniumwire", "pyppeteer", "puppeteer",
    "drissionpage", "undetected_chromedriver", "nodriver", "splinter",
    "webdriver_manager", "chromedriver_autoinstaller", "browser_use", "html2image", "imgkit",
})
_BROWSER_COMMAND = re.compile(
    r"(?:^|[\s/\\\"'])"
    r"(?:chromium(?:-browser)?|google-chrome(?:-stable|-beta|-unstable)?|chrome(?:-headless-shell)?|msedge|"
    r"chromedriver|geckodriver|firefox|playwright|puppeteer)"
    r"(?:\.exe|\.cmd)?(?=$|[\s\"';|&])", re.IGNORECASE,
)
_ENGINE_CONFIG_KEYS = frozenset({
    "browser", "chrome", "chromium", "playwright", "selenium", "puppeteer",
    "browser_path", "browser_executable", "chrome_path", "chromium_path", "chrome_binary",
    "executable_path", "browser_engine", "screenshot_engine", "cdp_url", "cdp_endpoint",
    "browser_ws_endpoint", "browserless_url", "browser_url", "kitesurf_ws_url",
})
_PROCESS_CALLS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen", "os.startfile", "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell", "shutil.which",
})
_GUIDANCE = "请使用公共截图服务 app.services.web_screenshots。"


def plugin_python_files(plugin_dir: Path) -> tuple[Path, ...]:
    """加载检查和缓存签名共用同一文件范围，辅助代码变化不能沿用上次检查结果。"""
    return tuple(sorted(
        path for path in plugin_dir.rglob("*.py")
        if not {"__pycache__", ".venv", "node_modules"}.intersection(path.relative_to(plugin_dir).parts)
    ))


def _manifest_has_browser_config(value: Any) -> bool:
    """递归检查运行配置和字段定义，只返回布尔值，不泄露字段默认值或服务地址。"""
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key).lower()
            if name in _ENGINE_CONFIG_KEYS or (name == "key" and isinstance(item, str) and item.lower() in _ENGINE_CONFIG_KEYS):
                return True
            if name in {"dependencies", "requirements", "packages"}:
                # 常见依赖声明不能把浏览器包或可执行文件重新装进插件。
                for package in item if isinstance(item, list) else [item]:
                    root = re.split(r"[<>=!~\[\s]", str(package).lower(), maxsplit=1)[0]
                    if root in _BROWSER_MODULES or _BROWSER_COMMAND.search(root):
                        return True
            if _manifest_has_browser_config(item):
                return True
    elif isinstance(value, list):
        return any(_manifest_has_browser_config(item) for item in value)
    return False


def _qualified_name(node: ast.AST, imports: dict[str, str]) -> str:
    """解析普通导入和 from 导入的别名，不执行插件源码。"""
    if isinstance(node, ast.Name):
        return imports.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        return f"{_qualified_name(node.value, imports)}.{node.attr}"
    return ""


def _static_strings(node: ast.AST, bindings: dict[str, ast.AST], seen: frozenset[str] = frozenset()) -> list[str]:
    """读取常量命令及简单变量赋值；不尝试执行函数或推断任意动态 Python。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in bindings and node.id not in seen:
        return _static_strings(bindings[node.id], bindings, seen | {node.id})
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [text for child in node.elts for text in _static_strings(child, bindings, seen)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_strings(node.left, bindings, seen)
        right = _static_strings(node.right, bindings, seen)
        return ["".join(left + right)]
    if isinstance(node, ast.JoinedStr):
        return ["".join(text for child in node.values for text in _static_strings(child, bindings, seen))]
    return []


def _python_errors(path: Path, plugin_dir: Path) -> list[str]:
    """检查真实 Python 调用语法，浏览器名出现在普通文案或 UA 中不会直接触发。"""
    relative = path.relative_to(plugin_dir).as_posix()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError, UnicodeError):
        return [f"{relative}: 无法解析 Python，不能完成公共截图规范检查。"]
    imports: dict[str, str] = {}
    bindings: dict[str, ast.AST] = {}
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                if alias.name.split(".")[0].lower() in _BROWSER_MODULES:
                    errors.append(f"{relative}:{node.lineno}: 不得导入浏览器自动化或安装包，{_GUIDANCE}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            if node.module.split(".")[0].lower() in _BROWSER_MODULES:
                errors.append(f"{relative}:{node.lineno}: 不得导入浏览器自动化或安装包，{_GUIDANCE}")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func, imports)
        if name in {"importlib.import_module", "__import__"} and node.args:
            if any(value.split(".")[0].lower() in _BROWSER_MODULES for value in _static_strings(node.args[0], bindings)):
                errors.append(f"{relative}:{node.lineno}: 不得动态导入浏览器包，{_GUIDANCE}")
        if name in _PROCESS_CALLS or name.startswith(("os.exec", "os.spawn")):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            if any(_BROWSER_COMMAND.search(value) for argument in arguments for value in _static_strings(argument, bindings)):
                errors.append(f"{relative}:{node.lineno}: 不得自行查找、安装或启动浏览器，{_GUIDANCE}")
    return errors


def validate_plugin_screenshot_policy(plugin_dir: Path, manifest: dict[str, Any]) -> tuple[str, ...]:
    """发现和交付共用这一条规范；只输出相对路径、行号与稳定原因。"""
    errors: list[str] = []
    if _manifest_has_browser_config(manifest):
        errors.append(f"config.toml: 不得声明浏览器依赖、地址或截图引擎配置，{_GUIDANCE}")
    for path in plugin_python_files(plugin_dir):
        errors.extend(_python_errors(path, plugin_dir))
    return tuple(errors)
