"""代理池真实浏览器回归；先构建 webui/frontend，需安装 Playwright 和 Chrome。"""
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:5100"


def serve_isolated(directory):
    sys.path.insert(0, str(ROOT))
    from config import env_loader

    # 子进程不复用生产配置、凭据或数据库；真实配置 API 写入临时文件。
    for key in env_loader.read_env_file():
        os.environ.pop(key, None)
    root = Path(directory)
    env_loader._ENV_PATH = root / ".env"
    env_loader._ENV_PATH.write_text(
        'PROXY_POOL="[]"\nRUNTIME_STORAGE_BACKEND="sqlite"\n', encoding="utf-8"
    )
    import config
    config.reload_all()
    from core.infra import db
    from core.infra.sqlite_store import SQLiteRuntimeStore

    for name, value in list(vars(db).items()):
        if isinstance(value, Path) and value.is_relative_to(ROOT):
            setattr(db, name, root / value.relative_to(ROOT))
    db._SQLITE_PATH_BINDINGS = {
        name: root / path.relative_to(ROOT)
        for name, path in db._SQLITE_PATH_BINDINGS.items()
    }
    db._SQLITE_STORE = SQLiteRuntimeStore(db._RUNTIME_DB)
    db._SQLITE_STORE.initialize()
    from webui.app import create_app
    create_app(auth_code="proxy-pool-browser-test").run(
        host="127.0.0.1", port=5100, use_reloader=False
    )


class ProxyPoolBrowserTests(unittest.TestCase):
    def test_edit_save_reload_and_clear(self):
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError:
            self.skipTest("需要 Playwright：pip install playwright")
        if not (ROOT / "webui/frontend/dist/index.html").exists():
            self.skipTest("需要先在 webui/frontend 执行 npm run build")
        with tempfile.TemporaryDirectory(prefix="proxy-pool-test-") as directory:
            server = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", directory],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):
                    self.assertIsNone(server.poll(), "隔离测试服务器启动失败（5100 可能被占用）")
                    try:
                        with urlopen(f"{BASE_URL}/login", timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("隔离测试服务器启动超时")
                with sync_playwright() as playwright, ExitStack() as stack:
                    browser = stack.enter_context(playwright.chromium.launch(channel="chrome"))
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.goto(f"{BASE_URL}/login")
                    page.locator('[name="auth_code"]').fill("proxy-pool-browser-test")
                    page.locator('button[type="submit"]').click()
                    page.get_by_role("button", name="配置", exact=True).click()
                    page.get_by_role("navigation", name="配置分组").get_by_role(
                        "button", name="代理池"
                    ).click()
                    field = page.get_by_label("代理池(每行一个)", exact=True)
                    expect(field).to_have_value("")
                    field.press("h")
                    expect(field).to_have_value("h")
                    field.press("t")
                    expect(field).to_have_value("ht")
                    proxies = ["http://127.0.0.1:18080", "socks5://127.0.0.1:18081"]
                    field.fill(f"  {proxies[0]}  \n\n{proxies[1]}\n")
                    save_button = page.locator('[id="config-代理池"]').get_by_role(
                        "button", name="保存修改", exact=True
                    )

                    def save():
                        with page.expect_response(
                            lambda response: response.url.endswith("/api/config")
                            and response.request.method == "POST"
                        ) as result:
                            save_button.click()
                        self.assertEqual(result.value.status, 200)
                        expect(save_button).to_be_disabled()
                        page.get_by_role("button", name="重新加载", exact=True).click()

                    save()
                    expect(field).to_have_value("\n".join(proxies))
                    field.press("End")
                    field.press("x")
                    expect(field).to_have_value("\n".join(proxies) + "x")
                    field.press("Backspace")
                    for width, height in [(1440, 1000), (390, 844)]:
                        page.set_viewport_size({"width": width, "height": height})
                        field.scroll_into_view_if_needed()
                        expect(field).to_be_visible()
                        expect(field).to_be_editable()
                        if output := os.environ.get("WEBUI_TEST_SCREENSHOTS"):
                            Path(output).mkdir(parents=True, exist_ok=True)
                            page.screenshot(path=str(Path(output) / f"proxy-pool-{width}.png"))
                    field.fill("")
                    save()
                    expect(field).to_have_value("")
                    values = page.request.get(f"{BASE_URL}/api/config").json()
                    self.assertEqual(next(item["value"] for item in values if item["key"] == "PROXY_POOL"), [])
                    self.assertIn('PROXY_POOL="[]"', (Path(directory) / ".env").read_text(encoding="utf-8"))
            finally:
                server.terminate()
                server.wait(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve_isolated(sys.argv[2])
    else:
        unittest.main()
